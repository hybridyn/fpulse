"""Phase 3 — route-posture ratchet.

Every mutating route (POST/PUT/PATCH/DELETE) should carry a dependency-based
auth/workspace gate (require_auth / current_workspace_id / require_min_rank /
an API-key check). This test FAILS if a NEW ungated mutating route appears
that isn't in the frozen baseline — forcing new routes to use a recognized
gate, or a conscious addition to the baseline with a reason.

In-body auth (calling current_user_optional(request) inside the handler) is
NOT detected here on purpose: a FastAPI dependency is auditable and hard to
forget; an in-body check is neither. New routes should prefer the dependency.
"""
import re

from fpulse.main import app
from fpulse.security.route_posture_baseline import KNOWN_UNGATED

_AUTH_RE = re.compile(
    r"auth|current_user|workspace|require_|_require|role|admin|api_key|apikey|token|plus|permission|verify_",
    re.I,
)


def _gated(route) -> bool:
    dep = getattr(route, "dependant", None)
    if not dep:
        return False
    stack = list(getattr(dep, "dependencies", []))
    while stack:
        d = stack.pop()
        c = getattr(d, "call", None)
        if c is not None and _AUTH_RE.search(getattr(c, "__name__", "") or ""):
            return True
        stack.extend(getattr(d, "dependencies", []))
    return False


def _current_ungated() -> set[str]:
    out: set[str] = set()
    for r in app.routes:
        methods = (getattr(r, "methods", set()) or set()) & {"POST", "PUT", "PATCH", "DELETE"}
        if not methods:
            continue
        if not _gated(r):
            for m in sorted(methods):
                out.add(f"{m} {getattr(r, 'path', '')}")
    return out


def test_no_new_ungated_mutating_route():
    new = _current_ungated() - set(KNOWN_UNGATED)
    assert not new, (
        "New mutating route(s) without a dependency-based auth gate. Add a gate "
        "(require_auth / current_workspace_id / require_min_rank) or, if the "
        "route is intentionally public, add it to "
        "fpulse.security.route_posture_baseline.KNOWN_UNGATED with a reason:\n  "
        + "\n  ".join(sorted(new))
    )


def test_baseline_not_stale():
    """Informational — surface baseline routes that have since gained a gate
    so the set can be trimmed. Never fails the build."""
    gone = set(KNOWN_UNGATED) - _current_ungated()
    if gone:
        print(f"[route-posture] {len(gone)} baseline routes now gated/removed — safe to trim.")
