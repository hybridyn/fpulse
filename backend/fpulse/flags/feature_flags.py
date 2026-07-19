"""
Feature flag registry.

Each new Week 1/2/Q2 behavior gets a flag here. Operators can flip flags:
  1. Permanently — via env var (e.g. FPULSE_FLAG_IDEMPOTENT_SINK=disable)
  2. At runtime — via /api/admin/flags (super_admin only, audit-logged)
  3. Gradually — via rollout percentage (0-100)

Rollback story:
  Old behavior:  redeploy binary.
  New behavior:  `POST /api/admin/flags {name: idempotent_sink, state: disable}` → takes effect on next request.

Precedence (highest first):
  1. Runtime override (stored in control.db.feature_flag_overrides)
  2. Env var (FPULSE_FLAG_{NAME}={enable|disable|shadow})
  3. Default (hardcoded per-flag)
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


class FlagState(str, enum.Enum):
    ENABLE = "enable"
    DISABLE = "disable"
    SHADOW = "shadow"   # run but do not change observable behavior (log-only)


@dataclass
class FeatureFlag:
    """
    A single feature flag with state + optional rollout percentage.

    shadow mode:
      The new code path runs and emits metrics/logs but the old behavior
      is still what the user sees. Used to collect "would this have dedupe-hit?"
      data before flipping to enable.

    rollout_pct:
      For enable mode, gradually roll out to a percentage of workspaces
      (deterministic hash of workspace_id). 100 = everyone, 0 = no one.
    """
    name: str
    description: str
    default_state: FlagState = FlagState.DISABLE
    rollout_pct: int = 0   # 0-100
    created_at: str = ""
    # mutable runtime state
    _runtime_state: Optional[FlagState] = None
    _runtime_rollout_pct: Optional[int] = None

    @property
    def state(self) -> FlagState:
        """Current effective state."""
        if self._runtime_state is not None:
            return self._runtime_state
        env_val = os.environ.get(f"FPULSE_FLAG_{self.name.upper()}")
        if env_val:
            try:
                return FlagState(env_val)
            except ValueError:
                log.warning("feature_flag_env_invalid name=%s value=%r", self.name, env_val)
        return self.default_state

    @property
    def effective_rollout_pct(self) -> int:
        if self._runtime_rollout_pct is not None:
            return self._runtime_rollout_pct
        env_val = os.environ.get(f"FPULSE_FLAG_{self.name.upper()}_ROLLOUT")
        if env_val:
            try:
                return max(0, min(100, int(env_val)))
            except ValueError:
                pass
        return self.rollout_pct

    @property
    def enabled(self) -> bool:
        """True if this code path should execute AND be observable."""
        return self.state == FlagState.ENABLE

    @property
    def shadow(self) -> bool:
        """True if this code path should execute for logging but not change behavior."""
        return self.state == FlagState.SHADOW

    def enabled_for(self, workspace_id: str) -> bool:
        """
        Check enabled with rollout pct. Deterministic per workspace so
        the same workspace always gets the same answer until flag state changes.
        """
        if self.state != FlagState.ENABLE:
            return False
        pct = self.effective_rollout_pct
        if pct >= 100:
            return True
        if pct <= 0:
            return False
        # Deterministic hash — same workspace_id always falls in same bucket
        h = int(hashlib.sha256(f"{self.name}:{workspace_id}".encode()).hexdigest()[:8], 16)
        return (h % 100) < pct


@dataclass
class FlagStore:
    """In-memory + optional SQLite-backed flag store."""
    _flags: dict[str, FeatureFlag] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _db = None   # optional; set via bind(conn)

    def bind(self, conn) -> None:
        """Attach a SQLite connection for persisted runtime overrides."""
        self._db = conn
        self._load_overrides()

    def register(self, flag: FeatureFlag) -> FeatureFlag:
        with self._lock:
            if flag.name in self._flags:
                log.debug("feature_flag_already_registered name=%s", flag.name)
                return self._flags[flag.name]
            self._flags[flag.name] = flag
            flag.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return flag

    def get(self, name: str) -> Optional[FeatureFlag]:
        return self._flags.get(name)

    def all(self) -> list[FeatureFlag]:
        return list(self._flags.values())

    def set_runtime_state(
        self,
        name: str,
        state: FlagState,
        rollout_pct: Optional[int] = None,
        actor_id: str = "unknown",
    ) -> FeatureFlag:
        """Change a flag at runtime. Called by /api/admin/flags."""
        with self._lock:
            flag = self._flags.get(name)
            if flag is None:
                raise KeyError(f"Unknown flag: {name}")
            flag._runtime_state = state
            if rollout_pct is not None:
                flag._runtime_rollout_pct = max(0, min(100, rollout_pct))
            self._persist_override(name, state, rollout_pct, actor_id)
            log.warning(
                "feature_flag_changed name=%s state=%s rollout=%s actor=%s",
                name, state.value, rollout_pct, actor_id,
            )
            return flag

    # ---- SQLite persistence (optional) ---------------------------------

    def _persist_override(self, name, state, rollout_pct, actor_id):
        if self._db is None:
            return
        try:
            self._db.execute("""
                INSERT INTO feature_flag_overrides (name, state, rollout_pct, updated_by, updated_at)
                VALUES (?, ?, ?, ?, datetime('now','utc'))
                ON CONFLICT(name) DO UPDATE SET
                    state = excluded.state,
                    rollout_pct = excluded.rollout_pct,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
            """, (name, state.value, rollout_pct, actor_id))
            self._db.commit()
        except Exception:
            log.exception("feature_flag_persist_failed name=%s", name)

    def _load_overrides(self):
        if self._db is None:
            return
        try:
            rows = self._db.execute(
                "SELECT name, state, rollout_pct FROM feature_flag_overrides"
            ).fetchall()
            for name, state, pct in rows:
                flag = self._flags.get(name)
                if flag is None:
                    continue
                try:
                    flag._runtime_state = FlagState(state)
                except ValueError:
                    continue
                flag._runtime_rollout_pct = pct
            log.info("feature_flags_loaded count=%d", len(rows))
        except Exception:
            log.exception("feature_flag_load_failed")


# ══════════════════════════════════════════════════════════════════════
# Registered flags (the canonical list)
# ══════════════════════════════════════════════════════════════════════

flags = FlagStore()

# ─── Week 1: kill-switches for new code paths ──────────────────────
flags.register(FeatureFlag(
    name="auth_deps_v2",
    description="Use auth/deps_v2.py (require_auth / require_role). "
                "Disable to fall back to the pre-existing auth layer. "
                "Emergency rollback only — A1-A5 reopen when disabled.",
    default_state=FlagState.ENABLE,
))

flags.register(FeatureFlag(
    name="duckdb_spill",
    description="Apply memory_limit / temp_directory pragmas to DuckDB "
                "executor. Disable only if spilling causes disk pressure "
                "incidents.",
    default_state=FlagState.ENABLE,
))

flags.register(FeatureFlag(
    name="workflow_steps_strict_persist",
    description="Enforce POST /api/workflows schema that requires `steps` "
                "field to persist. Disable = accept empty steps silently "
                "(pre-Week 1 behavior — DO NOT DO THIS).",
    default_state=FlagState.ENABLE,
))

# ─── Week 2: idempotency + notifier wrapper ─────────────────────────
flags.register(FeatureFlag(
    name="idempotent_sink",
    description="Route all sink dispatches (alerts/webhooks) through "
                "IdempotentNotifier. Shadow mode = log what WOULD dedupe "
                "without suppressing. Enable = actual suppression.",
    default_state=FlagState.SHADOW,   # ship as shadow first
))

flags.register(FeatureFlag(
    name="license_ed25519",
    description="Use Ed25519 license verification. During migration window, "
                "fall back to HMAC if set to 'disable'. Dual-path works via "
                "'shadow' — verify both, prefer Ed25519.",
    default_state=FlagState.SHADOW,
))

flags.register(FeatureFlag(
    name="observability_tracing",
    description="Enable OpenTelemetry span emission for FastAPI requests "
                "and DuckDB operations. Disable if OTel endpoint is "
                "degraded or causing latency.",
    default_state=FlagState.ENABLE,
))

# ─── Q2: moat features ──────────────────────────────────────────────
flags.register(FeatureFlag(
    name="audit_merkle_chain",
    description="Compute prev_hash + entry_hash on audit entries + hourly "
                "Ed25519 checkpoints. Disable to revert to legacy audit "
                "(append-only but not tamper-evident).",
    default_state=FlagState.DISABLE,
))

flags.register(FeatureFlag(
    name="file_per_workspace",
    description="Route store operations through WorkspaceDBRouter (per-"
                "workspace SQLite file). Dual-write during migration.",
    default_state=FlagState.DISABLE,
))

flags.register(FeatureFlag(
    name="zanzibar_acl",
    description="Consult auth_tuples table for fine-grained permission "
                "checks. Falls back to RBAC ladder when disabled.",
    default_state=FlagState.DISABLE,
))

flags.register(FeatureFlag(
    name="approval_diff_artifacts",
    description="Generate and store diff_artifact on approval submit. "
                "Approver UI shows side-by-side DAG diff + risk score.",
    default_state=FlagState.DISABLE,
))

flags.register(FeatureFlag(
    name="airgap_mode",
    description="Install socket monkey-patch to block non-whitelisted "
                "egress. Once enabled, disabling requires two-person "
                "approval.",
    default_state=FlagState.DISABLE,
))

flags.register(FeatureFlag(
    name="priority_pool_weighted",
    description="Use weighted round-robin (P1=16..P5=1) instead of "
                "strict priority. Prevents starvation.",
    default_state=FlagState.DISABLE,
))


# ══════════════════════════════════════════════════════════════════════
# Usage helpers
# ══════════════════════════════════════════════════════════════════════

def is_enabled(flag_name: str, workspace_id: Optional[str] = None) -> bool:
    """Convenience: check without holding a flag reference."""
    flag = flags.get(flag_name)
    if flag is None:
        log.warning("feature_flag_unknown name=%s — returning False", flag_name)
        return False
    if workspace_id:
        return flag.enabled_for(workspace_id)
    return flag.enabled


def is_shadow(flag_name: str) -> bool:
    flag = flags.get(flag_name)
    return flag is not None and flag.shadow
