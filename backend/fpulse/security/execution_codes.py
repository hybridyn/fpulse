"""One-time, run-bound execution authorization codes (Phase 7).

A short-lived, single-use code that binds an execution to a specific
``(user, workspace, project, pipeline, job_run, action)``. The point: a
stolen session or token alone should not let an attacker fire arbitrary
runs — each execution requires a *fresh* code minted by an authorized
initiation path, verified and consumed at the execution point.

Enforcement is gated by ``runtime_config.REQUIRE_EXECUTION_CODE``, which
defaults to ``IS_SERVER_MODE``: **ON in server mode** (exposed / multi-user)
and **OFF in local single-user mode**. ``FPULSE_REQUIRE_EXECUTION_CODE=1|0``
overrides either way. Every run-initiation path already mints a code (all 11
``execute_workflow`` call sites), so turning enforcement on fleet-wide is
safe — nothing silently breaks when the gate flips on.

OSS impl: an in-process store (single-node). A code expires if not consumed
within its TTL; consuming it marks it used (single-use). A process restart
drops in-flight codes — fail-safe, the run must be re-authorized. A durable,
cross-instance store is the Plus/HA follow-up.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass
class _CodeEntry:
    user_id: str
    workspace_id: str
    project_id: str
    pipeline_id: str
    job_run_id: str
    action: str
    expires_at: float
    used: bool = False


class ExecutionCodeStore:
    """Mint + verify one-time execution codes. Thread-safe."""

    def __init__(self, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._codes: dict[str, _CodeEntry] = {}
        self._lock = threading.Lock()

    def mint(
        self,
        *,
        user_id: str,
        workspace_id: str,
        pipeline_id: str,
        job_run_id: str,
        action: str = "run",
        project_id: str = "default",
        ttl_seconds: int | None = None,
    ) -> str:
        code = secrets.token_urlsafe(32)
        ttl = self._ttl if ttl_seconds is None else ttl_seconds
        exp = time.monotonic() + ttl
        with self._lock:
            self._prune_locked()
            self._codes[code] = _CodeEntry(
                user_id=user_id, workspace_id=workspace_id, project_id=project_id,
                pipeline_id=pipeline_id, job_run_id=job_run_id, action=action,
                expires_at=exp,
            )
        return code

    def verify_and_consume(
        self,
        code: str,
        *,
        workspace_id: str | None = None,
        pipeline_id: str | None = None,
        action: str | None = None,
    ) -> bool:
        """Return True at most once per code — only if unused, unexpired, and
        the bound fields match (when supplied). Marks the code used."""
        if not code:
            return False
        with self._lock:
            entry = self._codes.get(code)
            if entry is None or entry.used or entry.expires_at < time.monotonic():
                return False
            if workspace_id is not None and entry.workspace_id != workspace_id:
                return False
            if pipeline_id is not None and entry.pipeline_id != pipeline_id:
                return False
            if action is not None and entry.action != action:
                return False
            entry.used = True
            return True

    def _prune_locked(self) -> None:
        now = time.monotonic()
        dead = [c for c, e in self._codes.items() if e.used or e.expires_at < now]
        for c in dead:
            self._codes.pop(c, None)


_store = ExecutionCodeStore()


def get_execution_code_store() -> ExecutionCodeStore:
    return _store


def mint_for_run(workflow, *, user_id: str = "system", job_run_id: str = "", action: str = "run") -> str:
    """Convenience: mint a run-bound code from a Workflow object. Used at every
    run-initiation site so ``FPULSE_REQUIRE_EXECUTION_CODE`` can be enabled
    without any path erroring. Internal/nested runs pass ``user_id='system'``.
    Returns "" (harmless) when codes aren't required, to avoid churn."""
    from fpulse import runtime_config
    if not runtime_config.REQUIRE_EXECUTION_CODE:
        return ""
    return _store.mint(
        user_id=user_id or "system",
        workspace_id=getattr(workflow, "workspace_id", None) or "default",
        project_id=getattr(workflow, "project_id", None) or "default",
        pipeline_id=getattr(workflow, "id", None) or "",
        job_run_id=job_run_id or "",
        action=action,
    )


def enforce_execution_code(
    code: str,
    *,
    workspace_id: str | None = None,
    pipeline_id: str | None = None,
    action: str | None = None,
) -> None:
    """Gate an execution on a one-time code. No-op when
    ``FPULSE_REQUIRE_EXECUTION_CODE`` is off; otherwise raises
    ``PermissionError`` unless the code is valid and consumable.

    Call this at each run-initiation path once the codes are minted there.
    """
    from fpulse import runtime_config
    if not runtime_config.REQUIRE_EXECUTION_CODE:
        return
    if not _store.verify_and_consume(
        code, workspace_id=workspace_id, pipeline_id=pipeline_id, action=action
    ):
        raise PermissionError("A valid one-time execution code is required to run.")
