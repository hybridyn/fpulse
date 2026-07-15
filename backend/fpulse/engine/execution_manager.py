"""
ExecutionManager — the single structural gate for all spawning in F-Pulse.

Sprint 2 PR5, step 2 of the locked implementation order.

This module is deliberately boring. In step 2 it is a thin wrapper around
the existing WorkerPool for the pipeline path only; every other spawn
kind raises NotImplementedError. Steps 3–10 fill in subprocess, thread,
asyncio, and scheduled paths plus the global resource governor and
orphan reaper.

Design doc: DESIGN_EXECUTION_MANAGER.md at repo root. Read it before
editing this file.

Invariants (mandatory from day one, step 2 onwards):
  1. Singleton. Exactly one ExecutionManager per process. Second
     initialize() returns the same instance; re-entry from a forked
     child is rejected by assert_gate().
  2. Registry insert BEFORE primitive starts. spawn_*() writes the
     TaskHandle into _registry, then dispatches.
  3. Registry remove ON every terminal event. Completion callbacks and
     cancel paths both call _deregister().
  4. NotImplementedError, not silent success. Any spawn kind that is
     not yet wired up raises explicitly so callers cannot accidentally
     bypass the gate by expecting a no-op.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, TYPE_CHECKING

try:
    import psutil as _psutil
    _HAS_PSUTIL = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False

if TYPE_CHECKING:
    from fpulse.engine.worker_pool import WorkerPool, QueuedJob
    from fpulse.engine.subprocess_runner import SubprocessRunner, SubprocessResult
    from fpulse.engine.global_governor import GlobalResourceGovernor

logger = logging.getLogger("fpulse.execution_manager")


# Which kinds are naturally queueable — used by the governor to decide
# whether to accept or reject on yellow/orange tiers. Pipelines queue
# in WorkerPool; every other kind fails fast until a DeferredQueue
# lands in a later sprint (design doc §12, out-of-scope for PR5).
_QUEUEABLE_KINDS: frozenset[str] = frozenset({"pipeline"})

# Owners that represent the backend's own infrastructure. These are
# NEVER gated by the governor — if the scheduler / drift daemon /
# retention / backup / worker-pool watchdog can't start, nothing runs
# and the product is unusable. The governor exists to protect the
# host from USER workload overload, not to brick its own boot path.
# Pipeline-author and ad-hoc API spawns still go through admission.
_INFRASTRUCTURE_OWNERS: frozenset[str] = frozenset({
    "scheduler",        # PipelineScheduler
    "drift_daemon",     # drift scanner (commercial extension)
    "backup_scheduler", # BackupScheduler
    "retention",        # storage/retention.py scheduler
    "worker_pool",      # WorkerPool._timeout_watchdog
})


def _atexit_shutdown() -> None:
    """Last-resort cleanup for Python-exit paths that bypass FastAPI's
    lifespan shutdown (e.g., Ctrl+C breaking out of a test run). Kills
    any subprocess runners still alive; best-effort, exceptions are
    swallowed because atexit hooks should never raise."""
    instance = ExecutionManager._instance
    if instance is None:
        return
    try:
        instance.shutdown(timeout_s=5)
    except Exception:
        # atexit hooks must not raise; log is already best-effort here.
        pass


def _pid_alive_unlocked(pid: int) -> bool:
    """Check whether a PID refers to a live, non-zombie process.
    Name flags that this function does NOT acquire the manager's lock
    — callers must hold it themselves if they need consistency."""
    if not _HAS_PSUTIL or pid is None:
        return False
    try:
        proc = _psutil.Process(pid)
        if not proc.is_running():
            return False
        return proc.status() != _psutil.STATUS_ZOMBIE
    except _psutil.NoSuchProcess:
        return False
    except Exception:
        return False


# ── Data types (public) ────────────────────────────────────────────────

@dataclass
class ExecutionBudget:
    """Per-spawn resource ceiling. Step 2 records but does not enforce;
    step 3 enforces for subprocess kind; step 7 persists actuals to the
    executions table (schema v17)."""
    max_memory_mb: int = 512
    max_runtime_s: int = 3600
    max_attempts: int = 3


@dataclass
class TaskHandle:
    """Returned by every spawn_*() call. Opaque to callers — they pass
    it to cancel() / inspect() / stats(). The id is distinct from any
    underlying primitive's own id (e.g. QueuedJob.id)."""
    id: str
    kind: str                    # pipeline | subprocess | thread | asyncio | scheduled
    owner: str                   # pipeline_id | "scheduler" | "drift_daemon" | etc
    pid: int | None              # subprocess only, else None
    parent_pid: int
    started_at: datetime
    underlying_id: str | None = None  # QueuedJob.id for pipeline kind


@dataclass
class TaskRecord:
    """Registry entry. Step 2 carries the handle; step 3 populates
    memory_peak_mb / runtime_ms / exit_reason; step 5 adds the
    children list from psutil; step 6 adds the disk-backed log paths."""
    handle: TaskHandle
    status: str = "running"      # running | success | error | cancelled | killed | timed_out
    children: list[int] = field(default_factory=list)
    memory_peak_mb: float | None = None
    runtime_ms: float | None = None
    attempts: int = 1
    exit_reason: str | None = None
    stdout_log_path: str | None = None
    stderr_log_path: str | None = None


@dataclass
class ReapReport:
    """Returned by reap(). Step 2 always returns zero — step 5 implements
    the actual orphan walk."""
    orphans_killed: int = 0
    leaks_swept: int = 0
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExecutionStats:
    """Returned by stats(). Consumed by AdminPage and /metrics. Step 2
    exposes counts and the pool passthrough; step 4 adds the tier, step
    7 adds per-pipeline budget rollups."""
    by_kind: dict[str, int]
    caps: dict[str, int]
    pool_status: dict[str, Any]
    tier: str = "green"          # green | yellow | orange | red — set by step 4


# ── Default caps (chosen for 16 GB host, per design doc §5) ────────────

DEFAULT_CAPS: dict[str, int] = {
    # "pipeline" cap is owned by WorkerPool.max_workers; the entry here
    # is informational, not enforced by ExecutionManager in step 2.
    "pipeline": 0,
    "subprocess": 4,
    "thread": 16,
    "asyncio": 512,
    "scheduled": 32,
}


# ── ExecutionManager ───────────────────────────────────────────────────

class ExecutionManager:
    """Singleton. Every spawn path in F-Pulse goes through this class.

    Step 2 status: pipeline wrapper only. All other spawn_* raise.
    """

    _instance: "ExecutionManager | None" = None
    _singleton_pid: int | None = None
    _init_lock = threading.Lock()

    def __init__(
        self,
        *,
        worker_pool: "WorkerPool",
        caps: dict[str, int] | None = None,
        governor: "GlobalResourceGovernor | None" = None,
    ):
        self._pool = worker_pool
        self._caps = dict(DEFAULT_CAPS)
        if caps:
            self._caps.update(caps)
        # Reflect the pool's actual max as the pipeline cap for reporting.
        self._caps["pipeline"] = worker_pool.max_workers

        self._lock = threading.Lock()
        self._registry: dict[str, TaskRecord] = {}
        self._by_kind: dict[str, set[str]] = {k: set() for k in self._caps}
        self._by_owner: dict[str, set[str]] = {}
        self._by_pid: dict[int, str] = {}

        # Step 3: live subprocess runners keyed by handle_id. The
        # manager owns cancel + shutdown across all of them.
        self._subprocess_runners: dict[str, "SubprocessRunner"] = {}

        # Step 8: live thread / scheduled / asyncio primitives keyed by
        # handle_id. Threads and scheduled loops use a per-handle
        # `threading.Event` for cooperative cancel (Python threads
        # can't be forcibly stopped without corrupting state). Asyncio
        # tasks use the native `task.cancel()` which raises
        # CancelledError at the next await.
        self._threads: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._scheduled: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self._asyncio_tasks: dict[str, asyncio.Task] = {}

        # Step 4: system-level admission gate. Defaults (70/80/90 memory
        # + 85 CPU with 5pt hysteresis) are chosen for a 16 GB host;
        # override via constructor for production machines with more
        # headroom. Tests inject mocks.
        if governor is None:
            from fpulse.engine.global_governor import GlobalResourceGovernor
            governor = GlobalResourceGovernor()
        self._governor = governor

    # ── Singleton lifecycle ────────────────────────────────────────

    @classmethod
    def initialize(
        cls,
        *,
        worker_pool: "WorkerPool",
        caps: dict[str, int] | None = None,
        governor: "GlobalResourceGovernor | None" = None,
        register_atexit: bool = True,
    ) -> "ExecutionManager":
        """Create or return the singleton. Idempotent per-process.

        When `register_atexit` is True (production default), registers
        an atexit hook that calls shutdown() — a belt-and-suspenders
        safety net for Ctrl+C paths that bypass FastAPI's lifespan.
        Tests pass False so the hook doesn't leak across test runs."""
        with cls._init_lock:
            if cls._instance is not None:
                return cls._instance
            cls._instance = cls(worker_pool=worker_pool, caps=caps, governor=governor)
            cls._singleton_pid = os.getpid()
            logger.info(
                "ExecutionManager initialized (pid=%d, caps=%s)",
                cls._singleton_pid, cls._instance._caps,
            )
            if register_atexit:
                atexit.register(_atexit_shutdown)
            return cls._instance

    @classmethod
    def get(cls) -> "ExecutionManager":
        """Access the singleton. Raises if not yet initialized."""
        if cls._instance is None:
            raise RuntimeError(
                "ExecutionManager.initialize() must be called during "
                "app startup before any spawn. See main.py."
            )
        return cls._instance

    @classmethod
    def assert_gate(cls) -> None:
        """Runtime assertion that callers can import cheaply.

        Call sites that previously spawned directly (subprocess.Popen,
        threading.Thread, asyncio.create_task) should call this at
        module load as they migrate — it fails loud if the call is
        happening outside the singleton's process (forked worker, etc).
        """
        if cls._singleton_pid is None:
            raise RuntimeError(
                "ExecutionManager singleton not initialized — direct spawn forbidden"
            )
        if os.getpid() != cls._singleton_pid:
            raise RuntimeError(
                f"ExecutionManager gate: pid={os.getpid()} singleton_pid={cls._singleton_pid}"
            )

    @classmethod
    def _reset_for_tests(cls) -> None:
        """Test-only: wipe the singleton so tests can reinitialize with
        a fresh pool. Never called from production code."""
        with cls._init_lock:
            cls._instance = None
            cls._singleton_pid = None

    # ── Spawn paths ────────────────────────────────────────────────

    def spawn_pipeline(
        self,
        workflow_id: str,
        workflow_name: str,
        *,
        priority: int = 3,
        environment: str = "dev",
        budget: ExecutionBudget | None = None,
        fn: Callable,
        kwargs: dict | None = None,
        project_id: str = "default",
        workspace_id: str = "default",
        triggered_by: str = "manual",
        schedule_id: str | None = None,
        timeout_s: int | None = None,
    ) -> TaskHandle:
        """Submit a pipeline to the priority-queue pool.

        `timeout_s` (or `budget.max_runtime_s` if timeout_s is None and
        a budget is passed explicitly) is forwarded to the pool's
        timeout watchdog, which flags the job for cooperative abort
        once the deadline passes. Hard-kill of running pipeline
        threads awaits subprocess migration (post-PR5)."""
        effective_budget = budget or ExecutionBudget()
        self._admit(effective_budget, kind="pipeline", owner=workflow_id)

        # Resolve timeout: explicit timeout_s wins; else fall back to
        # the budget's max_runtime_s only when the caller passed a
        # non-default budget (a default budget's 3600s shouldn't
        # silently timeout every pipeline).
        resolved_timeout_s = timeout_s
        if resolved_timeout_s is None and budget is not None:
            resolved_timeout_s = budget.max_runtime_s

        job: "QueuedJob" = self._pool.submit(
            workflow_id=workflow_id,
            workflow_name=workflow_name,
            project_id=project_id,
            workspace_id=workspace_id,
            environment=environment,
            priority=priority,
            triggered_by=triggered_by,
            schedule_id=schedule_id,
            fn=fn,
            kwargs=kwargs or {},
            timeout_s=resolved_timeout_s,
        )

        handle = TaskHandle(
            id=uuid.uuid4().hex[:16],
            kind="pipeline",
            owner=workflow_id,
            pid=None,
            parent_pid=os.getpid(),
            started_at=job.queued_at,
            underlying_id=job.id,
        )
        self._register(handle)
        return handle

    def spawn_subprocess(
        self,
        name: str,
        argv: list[str],
        *,
        owner: str,
        budget: ExecutionBudget,
        env: dict[str, str] | None = None,
        monitor_interval_s: float = 1.0,
        log_dir: str | None = None,
        retry_on: set[str] | None = None,
    ) -> TaskHandle:
        """Spawn a capped child process. The child runs under a hard
        memory ceiling (setrlimit on Linux, psutil polling everywhere)
        and a wall-clock runtime cap. On exit, TaskRecord is populated
        with memory_peak_mb / runtime_ms / exit_reason.

        Step 9: bounded retry. Pass `retry_on={"error", "budget_runtime"}`
        (or similar) to automatically re-spawn when the exit reason
        matches, up to `budget.max_attempts` total tries. Retries run
        under the same TaskHandle — caller's reference is unchanged;
        `record.attempts` tracks the count. Default `retry_on=None`
        disables auto-retry — current callers see no behaviour change.
        `EXIT_CANCELLED` and `EXIT_KILLED_THROTTLE` never retry (the
        first is user intent, the second is system pressure that
        would just re-reject).
        """
        # Import here to keep the subprocess dependency lazy — tests
        # that only exercise the pipeline path don't pay for psutil.
        from fpulse.engine.subprocess_runner import (
            SubprocessRunner,
            SubprocessResult,
            EXIT_OK,
            EXIT_CANCELLED,
            EXIT_BUDGET_MEMORY,
            EXIT_BUDGET_RUNTIME,
            EXIT_KILLED_THROTTLE,
        )

        self._admit(budget, kind="subprocess", owner=owner)

        # Never retry user-cancel or system-pressure kills, regardless
        # of caller opt-in — these aren't transient failures.
        allowed_retry = set(retry_on or set())
        allowed_retry.discard(EXIT_CANCELLED)
        allowed_retry.discard(EXIT_KILLED_THROTTLE)
        allowed_retry.discard(EXIT_OK)

        handle_id = uuid.uuid4().hex[:16]
        handle = TaskHandle(
            id=handle_id,
            kind="subprocess",
            owner=owner,
            pid=None,
            parent_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        )

        # Atomically: check cap + reserve the slot. Holding the lock
        # across the check-and-insert is required so concurrent spawns
        # cannot both slip past the cap. runner.start() happens after
        # the lock is released — it can block briefly and we don't
        # want spawn serialization.
        with self._lock:
            active = len(self._by_kind.get("subprocess", set()))
            cap = self._caps.get("subprocess", 0)
            if cap > 0 and active >= cap:
                raise RuntimeError(
                    f"subprocess cap reached ({active} >= {cap}) for owner={owner!r}"
                )
            self._registry[handle_id] = TaskRecord(handle=handle, attempts=0)
            self._by_kind.setdefault("subprocess", set()).add(handle_id)
            self._by_owner.setdefault(owner, set()).add(handle_id)

        # Step 6: resolve log dir once; attempt number is appended to
        # the log name so each retry writes to its own file.
        resolved_log_dir = log_dir
        if resolved_log_dir is None:
            env_dir = os.environ.get("FPULSE_DATA_DIR")
            if env_dir:
                resolved_log_dir = os.path.join(env_dir, "exec_logs")

        status_map = {
            EXIT_OK: "success",
            EXIT_CANCELLED: "cancelled",
            EXIT_BUDGET_MEMORY: "killed",
            EXIT_BUDGET_RUNTIME: "timed_out",
            EXIT_KILLED_THROTTLE: "killed",
        }

        def _launch(attempt_num: int) -> None:
            """Spawn one attempt. On exit, either retry (another
            _launch with attempt_num+1) or finalize the record."""

            def _on_exit(result: "SubprocessResult") -> None:
                should_retry = (
                    result.exit_reason in allowed_retry
                    and attempt_num < budget.max_attempts
                )

                if should_retry:
                    # Update the live record with this attempt's outcome
                    # but keep the registry entry so the caller's handle
                    # remains valid across the retry.
                    with self._lock:
                        record = self._registry.get(handle_id)
                        if record is not None:
                            record.attempts = attempt_num
                            record.exit_reason = result.exit_reason
                            record.memory_peak_mb = result.memory_peak_mb
                            record.runtime_ms = result.runtime_ms
                            record.stdout_log_path = result.stdout_log_path
                            record.stderr_log_path = result.stderr_log_path
                        # Clear by_pid + runner ref for this attempt so
                        # the next _launch can populate them fresh.
                        if result.pid is not None:
                            self._by_pid.pop(result.pid, None)
                        self._subprocess_runners.pop(handle_id, None)

                    logger.info(
                        "subprocess retry: name=%s owner=%s attempt=%d/%d reason=%s",
                        name, owner, attempt_num, budget.max_attempts,
                        result.exit_reason,
                    )
                    try:
                        _launch(attempt_num + 1)
                    except Exception:
                        logger.exception(
                            "subprocess retry spawn failed — finalizing as error"
                        )
                        with self._lock:
                            record = self._registry.get(handle_id)
                            if record is not None:
                                record.status = "error"
                                record.exit_reason = "retry_spawn_failed"
                                record.attempts = attempt_num
                        self._deregister(handle_id)
                    return

                # Terminal: either success, non-retryable reason, or
                # max_attempts exhausted.
                final_status = status_map.get(result.exit_reason, "error")
                with self._lock:
                    record = self._registry.get(handle_id)
                    if record is not None:
                        record.status = final_status
                        record.memory_peak_mb = result.memory_peak_mb
                        record.runtime_ms = result.runtime_ms
                        record.exit_reason = result.exit_reason
                        record.stdout_log_path = result.stdout_log_path
                        record.stderr_log_path = result.stderr_log_path
                        record.attempts = attempt_num
                    self._subprocess_runners.pop(handle_id, None)

                logger.info(
                    "subprocess exit: name=%s owner=%s pid=%s reason=%s "
                    "peak_mb=%s runtime_ms=%.1f attempt=%d/%d",
                    name, owner, result.pid, result.exit_reason,
                    result.memory_peak_mb, result.runtime_ms,
                    attempt_num, budget.max_attempts,
                )
                self._deregister(handle_id)

            log_name = f"{owner}_{handle_id}_a{attempt_num}"
            runner = SubprocessRunner(
                argv=argv,
                max_memory_mb=budget.max_memory_mb,
                max_runtime_s=budget.max_runtime_s,
                env=env,
                on_exit=_on_exit,
                monitor_interval_s=monitor_interval_s,
                log_dir=resolved_log_dir,
                log_name=log_name,
            )

            def _on_spawn(pid: int) -> None:
                # Registers _by_pid + _subprocess_runners BEFORE the
                # monitor thread starts — guarantees the indexes are
                # populated when on_exit fires, even for very-fast-exit
                # children. Also updates the record: log paths point
                # to THIS attempt's files; attempts reflects the live
                # count in case anyone inspects mid-run.
                handle.pid = pid
                with self._lock:
                    self._by_pid[pid] = handle_id
                    self._subprocess_runners[handle_id] = runner
                    record = self._registry.get(handle_id)
                    if record is not None:
                        record.stdout_log_path = runner.stdout_log_path
                        record.stderr_log_path = runner.stderr_log_path
                        record.attempts = attempt_num

            pid = runner.start(on_spawn=_on_spawn)
            logger.info(
                "subprocess spawned: name=%s owner=%s pid=%d attempt=%d/%d "
                "cap=%dMB timeout=%ds",
                name, owner, pid, attempt_num, budget.max_attempts,
                budget.max_memory_mb, budget.max_runtime_s,
            )

        try:
            _launch(1)
        except Exception:
            # Attempt 1 spawn failed outright — roll back the
            # reservation so a failed spawn doesn't eat a cap slot.
            self._deregister(handle_id)
            with self._lock:
                self._subprocess_runners.pop(handle_id, None)
            raise

        return handle

    def spawn_thread(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        owner: str,
        daemon: bool = True,
        kwargs: dict | None = None,
        stop_event_kwarg: str | None = None,
    ) -> TaskHandle:
        """Spawn a counted thread.

        `stop_event_kwarg`, if provided, names a kwarg that fn accepts
        for receiving the handle's cancel Event — the canonical way
        for a thread to observe a cooperative cancel. Example:
            def _worker(stop_event: threading.Event):
                while not stop_event.is_set():
                    ...
            mgr.spawn_thread("my-worker", _worker, owner="drift",
                             stop_event_kwarg="stop_event")

        Python cannot forcibly kill a thread without risking corrupted
        state (open files, held locks, partial writes to DuckDB). So
        cancel() sets the event and the fn must check it.
        """
        self._admit(ExecutionBudget(), kind="thread", owner=owner)

        handle_id, handle = self._reserve_slot("thread", owner)

        stop_event = threading.Event()
        resolved_kwargs: dict[str, Any] = dict(kwargs or {})
        if stop_event_kwarg is not None:
            resolved_kwargs[stop_event_kwarg] = stop_event

        def _wrapper() -> None:
            try:
                fn(**resolved_kwargs)
            except Exception:
                logger.exception("spawn_thread %s (%s) raised", handle_id, owner)
            finally:
                with self._lock:
                    self._threads.pop(handle_id, None)
                self._deregister(handle_id)

        thread = threading.Thread(
            target=_wrapper,
            name=f"fpulse-{owner}-{handle_id[:8]}",
            daemon=daemon,
        )
        with self._lock:
            self._threads[handle_id] = (thread, stop_event)
        thread.start()
        return handle

    def spawn_asyncio_task(
        self,
        coro: Coroutine,
        *,
        owner: str,
        name: str,
    ) -> TaskHandle:
        """Spawn a counted asyncio.Task. Must be called from an async
        context with a running event loop. Cancel uses the native
        task.cancel() which raises CancelledError at the next await.

        On completion (success, exception, or cancel), the task is
        deregistered via a done_callback."""
        self._admit(ExecutionBudget(), kind="asyncio", owner=owner)

        handle_id, handle = self._reserve_slot("asyncio", owner)

        task = asyncio.create_task(coro, name=f"fpulse-{owner}-{handle_id[:8]}")

        def _done(t: asyncio.Task) -> None:
            with self._lock:
                self._asyncio_tasks.pop(handle_id, None)
            self._deregister(handle_id)
            # Don't log CancelledError — routine cancellation is the
            # caller's intent. Other exceptions did bubble; log them.
            exc = None
            try:
                exc = t.exception()
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                return
            if exc is not None:
                logger.exception(
                    "spawn_asyncio_task %s (%s) raised: %s",
                    handle_id, owner, exc,
                )

        task.add_done_callback(_done)

        with self._lock:
            self._asyncio_tasks[handle_id] = task

        return handle

    def spawn_scheduled(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        owner: str,
        interval_seconds: float,
        kwargs: dict | None = None,
        initial_delay_seconds: float = 0.0,
    ) -> TaskHandle:
        """Spawn a counted scheduled task — a background thread that
        invokes fn every `interval_seconds` until cancelled. Each
        iteration is independent; exceptions are logged but the loop
        continues (prevents one bad tick from killing the schedule).

        `initial_delay_seconds` defers the first invocation so cold-start
        latency isn't amplified by an immediate heavy scan (e.g. drift
        daemon's full-DB sweep). Cancel is honoured mid-delay — the
        stop_event wait returns early when set."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0")
        if initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be >= 0")
        self._admit(ExecutionBudget(), kind="scheduled", owner=owner)

        handle_id, handle = self._reserve_slot("scheduled", owner)

        stop_event = threading.Event()
        resolved_kwargs: dict[str, Any] = dict(kwargs or {})

        def _loop() -> None:
            try:
                # Optional initial delay before the first tick.
                if initial_delay_seconds > 0:
                    if stop_event.wait(timeout=initial_delay_seconds):
                        return
                while not stop_event.is_set():
                    try:
                        fn(**resolved_kwargs)
                    except Exception:
                        logger.exception(
                            "scheduled task %s (%s) iteration raised — continuing",
                            handle_id, owner,
                        )
                    # Event.wait returns True when set — exits the
                    # loop promptly on cancel() without waiting out
                    # the full interval.
                    if stop_event.wait(timeout=interval_seconds):
                        return
            finally:
                with self._lock:
                    self._scheduled.pop(handle_id, None)
                self._deregister(handle_id)

        thread = threading.Thread(
            target=_loop,
            name=f"fpulse-sched-{owner}-{handle_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._scheduled[handle_id] = (thread, stop_event)
        thread.start()
        return handle

    def _reserve_slot(self, kind: str, owner: str) -> tuple[str, TaskHandle]:
        """Allocate a handle, check the cap, and insert into registry
        + per-kind + per-owner indices atomically. Returns the ID and
        handle. Raises RuntimeError on cap breach. Internal helper
        shared by spawn_thread / spawn_asyncio_task / spawn_scheduled
        so the check-and-reserve dance isn't duplicated."""
        handle_id = uuid.uuid4().hex[:16]
        handle = TaskHandle(
            id=handle_id,
            kind=kind,
            owner=owner,
            pid=None,
            parent_pid=os.getpid(),
            started_at=datetime.now(timezone.utc),
        )
        with self._lock:
            active = len(self._by_kind.get(kind, set()))
            cap = self._caps.get(kind, 0)
            if cap > 0 and active >= cap:
                raise RuntimeError(
                    f"{kind} cap reached ({active} >= {cap}) for owner={owner!r}"
                )
            self._registry[handle_id] = TaskRecord(handle=handle)
            self._by_kind.setdefault(kind, set()).add(handle_id)
            self._by_owner.setdefault(owner, set()).add(handle_id)
        return handle_id, handle

    # ── Lifecycle ──────────────────────────────────────────────────

    def cancel_by_id(self, handle_id: str) -> tuple[bool, str]:
        """Look up a handle by ID and cancel it.

        Returns (ok, reason): ok=True if cancel succeeded, False if
        not (handle unknown, already finished, or runner missing).
        reason is a short diagnostic string for the API response.

        This is the HTTP-friendly wrapper around cancel(handle) — the
        admin route (`POST /api/admin/execution/{handle_id}/cancel`)
        doesn't have a TaskHandle, only its ID from a prior /inspect.
        """
        with self._lock:
            record = self._registry.get(handle_id)
        if record is None:
            return False, "handle not found in registry"
        try:
            ok = self.cancel(record.handle)
        except NotImplementedError as exc:
            return False, f"cancel not supported: {exc}"
        if ok:
            return True, "cancelled"
        return False, "primitive did not acknowledge cancel"

    def cancel(self, handle: TaskHandle) -> bool:
        """Cancel a running or queued task. Returns True if the
        underlying primitive acknowledged the cancel."""
        if handle.kind == "pipeline":
            if handle.underlying_id is None:
                return False
            ok = self._pool.cancel(handle.underlying_id)
            if ok:
                self._mark_status(handle.id, "cancelled")
                self._deregister(handle.id)
            return ok

        if handle.kind == "subprocess":
            with self._lock:
                runner = self._subprocess_runners.get(handle.id)
            if runner is None:
                return False
            # Runner.cancel() triggers _terminate_tree; the monitor
            # thread then fires on_exit which deregisters. We don't
            # deregister here to avoid racing the callback.
            return runner.cancel(timeout_s=3.0)

        if handle.kind in ("thread", "scheduled"):
            # Cooperative cancel: set the event. The wrapper / loop
            # deregisters when the fn returns. No join here — cancel
            # is best-effort; shutdown() handles the join.
            with self._lock:
                pair = (
                    self._threads.get(handle.id)
                    if handle.kind == "thread"
                    else self._scheduled.get(handle.id)
                )
            if pair is None:
                return False
            _, stop_event = pair
            was_set = stop_event.is_set()
            stop_event.set()
            return not was_set

        if handle.kind == "asyncio":
            with self._lock:
                task = self._asyncio_tasks.get(handle.id)
            if task is None or task.done():
                return False
            return task.cancel()

        raise NotImplementedError(
            f"cancel for kind={handle.kind!r} not implemented"
        )

    def reap(self) -> ReapReport:
        """Walk the process tree, kill orphans, sweep leaks, refresh
        children on live TaskRecords.

        Orphan = a child of this process not tracked in `_by_pid` and
        not a descendant of any tracked child. Killed via SIGTERM →
        wait → SIGKILL.

        Leak = a subprocess TaskRecord whose pid is no longer alive
        but on_exit never fired. Swept from the registry.

        Safe to call concurrently with monitor threads: all registry
        mutations are under `_lock`, and `_deregister` tolerates
        already-removed entries."""
        if not _HAS_PSUTIL:
            return ReapReport()

        my_pid = os.getpid()
        try:
            me = _psutil.Process(my_pid)
            live_children = me.children(recursive=True)
        except _psutil.NoSuchProcess:
            return ReapReport()

        with self._lock:
            tracked_pids = set(self._by_pid.keys())
            registry_snapshot = list(self._registry.items())

        # Allowed set = tracked PIDs + all their descendants. A live
        # child not in this set is an orphan.
        allowed_pids = set(tracked_pids)
        for tracked_pid in tracked_pids:
            try:
                for desc in _psutil.Process(tracked_pid).children(recursive=True):
                    allowed_pids.add(desc.pid)
            except _psutil.NoSuchProcess:
                pass

        orphan_procs = [c for c in live_children if c.pid not in allowed_pids]

        # SIGTERM the orphans, wait briefly, SIGKILL the holdouts.
        for c in orphan_procs:
            try:
                c.terminate()
                logger.warning("reap: terminating orphan pid=%d name=%s", c.pid, c.name())
            except _psutil.NoSuchProcess:
                pass
            except Exception:
                logger.exception("reap: terminate failed for pid=%d", c.pid)

        orphans_killed = 0
        if orphan_procs:
            gone, alive = _psutil.wait_procs(orphan_procs, timeout=3)
            orphans_killed = len(gone)
            for c in alive:
                try:
                    c.kill()
                    orphans_killed += 1
                    logger.warning("reap: killed stubborn orphan pid=%d", c.pid)
                except _psutil.NoSuchProcess:
                    pass

        # Leak sweep: handles whose primitive is gone without on_exit.
        # Also refresh children on live subprocess records (observability).
        leaks_swept = 0
        with self._lock:
            for handle_id, record in registry_snapshot:
                h = record.handle
                if h.kind != "subprocess" or h.pid is None:
                    continue
                if not _pid_alive_unlocked(h.pid):
                    # Process is gone but record lingers — monitor thread
                    # must have died or been blocked. Sweep manually.
                    if handle_id in self._registry:
                        self._by_kind.get(h.kind, set()).discard(handle_id)
                        self._by_owner.get(h.owner, set()).discard(handle_id)
                        self._by_pid.pop(h.pid, None)
                        self._subprocess_runners.pop(handle_id, None)
                        del self._registry[handle_id]
                        leaks_swept += 1
                        logger.warning(
                            "reap: swept leaked record handle_id=%s pid=%s owner=%s",
                            handle_id, h.pid, h.owner,
                        )
                else:
                    # Still running — refresh children list for the UI.
                    try:
                        record.children = [
                            c.pid for c in _psutil.Process(h.pid).children(recursive=True)
                        ]
                    except _psutil.NoSuchProcess:
                        record.children = []

        logger.info(
            "reap: orphans_killed=%d leaks_swept=%d",
            orphans_killed, leaks_swept,
        )
        return ReapReport(
            orphans_killed=orphans_killed,
            leaks_swept=leaks_swept,
        )

    def shutdown(self, timeout_s: float = 30) -> None:
        """Graceful drain. Cancels every live spawn across all kinds
        (subprocess, thread, scheduled, asyncio), then stops the worker
        pool. Step 5 extended this with subprocess handling; step 8
        adds thread / scheduled / asyncio cancellation + join."""
        logger.info("ExecutionManager.shutdown(timeout_s=%s)", timeout_s)

        # Snapshot everything so we can iterate without holding the
        # lock — each cancel triggers its own callback that may take
        # the lock to deregister.
        with self._lock:
            runners = list(self._subprocess_runners.values())
            threads = list(self._threads.items())
            scheduled = list(self._scheduled.items())
            asyncio_tasks = list(self._asyncio_tasks.items())

        # Subprocesses (step 5 behaviour).
        if runners:
            logger.info("shutdown: cancelling %d live subprocess(es)", len(runners))
            grace = max(1.0, timeout_s / max(1, len(runners)))
            for runner in runners:
                try:
                    runner.cancel(timeout_s=grace)
                except Exception:
                    logger.exception("shutdown: runner.cancel failed")

        # Threads + scheduled: signal stop, then join with a bounded
        # per-primitive grace window so one stuck thread can't hold
        # the whole shutdown hostage.
        thread_grace = max(0.5, min(5.0, timeout_s / max(1, len(threads) + len(scheduled))))
        for handle_id, (thread, stop_event) in threads:
            stop_event.set()
        for handle_id, (thread, stop_event) in scheduled:
            stop_event.set()
        for handle_id, (thread, _) in threads + scheduled:
            try:
                thread.join(timeout=thread_grace)
                if thread.is_alive():
                    logger.warning(
                        "shutdown: thread %s did not exit within %.1fs",
                        thread.name, thread_grace,
                    )
            except Exception:
                logger.exception("shutdown: thread join failed")

        # Asyncio tasks: cancel and optionally await if a running loop
        # is available. In production shutdown, uvicorn's lifespan
        # runs inside its own loop so asyncio.get_running_loop works.
        # In a teardown fixture called synchronously (tests), we just
        # cancel and don't await.
        if asyncio_tasks:
            logger.info("shutdown: cancelling %d asyncio task(s)", len(asyncio_tasks))
            for handle_id, task in asyncio_tasks:
                try:
                    if not task.done():
                        task.cancel()
                except Exception:
                    logger.exception("shutdown: task.cancel failed")

        try:
            self._pool.stop()
        except Exception as exc:
            logger.warning("WorkerPool.stop() raised during shutdown: %s", exc)

    # ── Observability ──────────────────────────────────────────────

    def stats(self) -> ExecutionStats:
        """Summary for AdminPage + /metrics."""
        with self._lock:
            by_kind = {k: len(v) for k, v in self._by_kind.items()}
        pool_status = self._pool.get_status()
        tier = "green"
        if self._governor is not None:
            snap = self._governor.snapshot()
            tier = snap.get("active_tier", "green")
        return ExecutionStats(
            by_kind=by_kind,
            caps=dict(self._caps),
            pool_status=pool_status,
            tier=tier,
        )

    def inspect(self, owner: str | None = None) -> list[TaskRecord]:
        """Return registry entries, optionally filtered by owner."""
        with self._lock:
            if owner is None:
                return list(self._registry.values())
            ids = self._by_owner.get(owner, set())
            return [self._registry[i] for i in ids if i in self._registry]

    # ── Internals ──────────────────────────────────────────────────

    def _admit(
        self,
        budget: ExecutionBudget,
        *,
        kind: str,
        owner: str | None = None,
    ) -> None:
        """System-level admission gate. Consults the GlobalResourceGovernor
        and raises GovernorRejection if the spawn must not proceed.
        Per-workflow concurrency + queue depth stays with WorkerPool's
        ResourceGovernor — the two are orthogonal.

        Infrastructure owners (scheduler / drift_daemon / backup_scheduler
        / retention / worker_pool) bypass the governor unconditionally —
        they're the backend's own boot path, not user workload."""
        _ = budget
        if self._governor is None:
            return
        if owner is not None and owner in _INFRASTRUCTURE_OWNERS:
            return
        queueable = kind in _QUEUEABLE_KINDS
        self._governor.check(kind, queueable=queueable)

    def _register(self, handle: TaskHandle) -> None:
        with self._lock:
            self._registry[handle.id] = TaskRecord(handle=handle)
            self._by_kind.setdefault(handle.kind, set()).add(handle.id)
            self._by_owner.setdefault(handle.owner, set()).add(handle.id)
            if handle.pid is not None:
                self._by_pid[handle.pid] = handle.id

    def _deregister(self, handle_id: str) -> None:
        with self._lock:
            record = self._registry.pop(handle_id, None)
            if record is None:
                return
            h = record.handle
            self._by_kind.get(h.kind, set()).discard(handle_id)
            self._by_owner.get(h.owner, set()).discard(handle_id)
            if h.pid is not None:
                self._by_pid.pop(h.pid, None)

    def _mark_status(self, handle_id: str, status: str) -> None:
        with self._lock:
            record = self._registry.get(handle_id)
            if record is not None:
                record.status = status
