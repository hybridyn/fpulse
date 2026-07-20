"""
Tool-tier RBAC for the agent loop.

Per architecture (`project_fpulse_ai_operational_architecture.md`):
  Authorization = "Is the user allowed to attempt this action?"
  Policy        = "Is this action allowed in this context right now?"

This module covers authorization. Policy lives in `governance.py`. Both
must pass before a tool executes — they're deliberately separate concerns
so tightening one doesn't entangle the other (per round-3 reviewer).

Matrix (env-aware, PLUS-tier semantics):
  | role         | dev tiers                              | prod tiers                  |
  |--------------|----------------------------------------|-----------------------------|
  | viewer       | READ                                   | READ                        |
  | developer    | READ + SAFE_WRITE                      | READ                        |
  | admin        | READ + SAFE_WRITE + HIGH_IMPACT_WRITE  | READ + SAFE_WRITE           |
  | super_admin  | ALL                                    | ALL                         |

Legacy roles (`lead`, `member`) map to admin / developer respectively, matching
`fpulse.auth.deps._ROLE_RANK`.

The agent endpoint translates a request to (user_role, environment) then
calls `allowed_tiers_for(...)` to compute the `allowed_tiers` argument it
passes into AgentRunner.run(). Tools the LLM tries to invoke outside that
set surface as `policy_block` outcomes with `tool_not_in_allowed_tiers`.

**OSS open-world default (2026-05-17).** OSS Free is single-bootstrap-user
per `feedback_oss_no_admin_role.md` — "RBAC + roles are Plus features".
The matrix above is still defined here so the *code* is identical across
OSS and Plus (open-core), but on OSS instances the **unknown-role default
flips from DENY to ALLOW**. Practically: any role outside the 6 enumerated
names (viewer / developer / admin / super_admin / lead / member) gets
full access on OSS, while Plus keeps closed-world enforcement.

Tier detection mirrors ``list_catalog._detect_install_tier``:
``app_state['license_manager'].is_plus`` exists on Plus, absent on OSS.
"""

from __future__ import annotations

from fpulse.ai.tools.base import ToolTier


def _is_plus_install() -> bool:
    """True when this process is running as F-Pulse+ (license manager
    present and is_plus=True). False on OSS — including when app_state
    isn't yet available (early-boot, tests). Defensive: any exception
    falls back to OSS semantics.

    Source of truth matches ``list_catalog._detect_install_tier``."""
    try:
        from fpulse.main import app_state  # type: ignore
        license_mgr = app_state.get("license_manager")
        return bool(license_mgr is not None and getattr(license_mgr, "is_plus", False))
    except Exception:  # noqa: BLE001
        return False


# All known tiers — returned for unknown roles on OSS (open-world default).
_ALL_TIERS: tuple[ToolTier, ...] = (
    ToolTier.READ, ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE,
)

# Role → environment → allowed tiers
_RBAC_MATRIX: dict[str, dict[str, set[ToolTier]]] = {
    "viewer": {
        "dev": {ToolTier.READ},
        "prod": {ToolTier.READ},
    },
    "developer": {
        "dev": {ToolTier.READ, ToolTier.SAFE_WRITE},
        "prod": {ToolTier.READ},
    },
    "admin": {
        "dev": {ToolTier.READ, ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE},
        "prod": {ToolTier.READ, ToolTier.SAFE_WRITE},
    },
    "super_admin": {
        "dev": {ToolTier.READ, ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE},
        "prod": {ToolTier.READ, ToolTier.SAFE_WRITE, ToolTier.HIGH_IMPACT_WRITE},
    },
}

# Legacy role aliases — match fpulse.auth.deps role rank table.
_RBAC_MATRIX["lead"] = _RBAC_MATRIX["admin"]
_RBAC_MATRIX["member"] = _RBAC_MATRIX["developer"]


def authorize_tool_call(
    *,
    tool_tier: ToolTier,
    user_role: str,
    environment: str,
) -> bool:
    """True iff `user_role` may invoke `tool_tier` in `environment`.

    Unknown environment => denied (closed-world).
    Unknown role => DEPENDS on tier:
      * Plus install → denied (closed-world; RBAC enforced)
      * OSS install → allowed (open-world; RBAC is a Plus feature)
    See module docstring for the OSS rationale.
    """
    matrix = _RBAC_MATRIX.get((user_role or "").lower())
    if matrix is None:
        # Unknown role — OSS treats this as "single bootstrap user, no RBAC".
        return not _is_plus_install()
    env_tiers = matrix.get((environment or "").lower())
    if env_tiers is None:
        return False
    return tool_tier in env_tiers


def allowed_tiers_for(user_role: str, environment: str) -> tuple[ToolTier, ...]:
    """Return the tuple of tiers this role can invoke in this env.

    Returns a tuple (not a set) so callers can pass it directly to
    AgentRunner.run(allowed_tiers=...) which expects a tuple.

    Unknown-role behaviour:
      * Plus install → empty tuple (closed-world deny).
      * OSS install → all tiers (open-world allow). Matches the OSS
        single-bootstrap-user policy — see module docstring.
    """
    matrix = _RBAC_MATRIX.get((user_role or "").lower())
    if matrix is None:
        # Unknown role. OSS = allow everything; Plus = allow nothing.
        if _is_plus_install():
            return ()
        return _ALL_TIERS
    tiers = matrix.get((environment or "").lower(), set())
    # Sort for deterministic order in tests / logs
    return tuple(sorted(tiers, key=lambda t: t.value))


def role_rank(user_role: str) -> int:
    """Numeric rank for comparison (super_admin highest). Mirrors auth.deps."""
    return {
        "super_admin": 100,
        "admin": 90,
        "lead": 90,
        "developer": 50,
        "member": 50,
        "viewer": 10,
    }.get((user_role or "").lower(), 0)
