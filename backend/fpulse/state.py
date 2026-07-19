"""Typed accessors for ``fpulse.main.app_state``.

Background (2026-05-22 fix). Routers and tools across the codebase resolve
shared singletons through bare dict lookups:

    from fpulse.main import app_state
    store = app_state["execution_store"]

That pattern fails two ways:

1. **Order-dependent test failures.** If a test runs before the lifespan
   has populated ``app_state``, the lookup raises ``KeyError`` — and the
   message ("'execution_store'") doesn't tell anyone the actual cause
   (lifespan didn't run, or another test mutated the dict).
2. **Silent contract drift.** Callers can ask for arbitrary keys; there's
   no central record of which app_state entries are required vs.
   optional, so new modules accumulate ad-hoc keys.

This module gives every well-known app_state entry a typed accessor
that raises ``RuntimeError`` with an actionable message when the lifespan
hasn't populated it. Callers should prefer these over bare lookups.

    from fpulse.state import get_execution_store
    store = get_execution_store()

For optional entries (e.g. the RAG embedder, which is only present when
RAG is configured), the helpers expose ``try_get_*`` variants that return
``None`` rather than raising.

Migration is incremental — adding a helper does not retroactively break
the existing bare lookups in ``backend/fpulse/api/*.py``. Move call sites
over as you touch each file.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING


if TYPE_CHECKING:
    # All `TYPE_CHECKING` imports avoid the runtime import cycle through
    # fpulse.main (which is where the lifespan ultimately populates these
    # singletons). The accessors return the concrete types only when an
    # IDE / mypy is inspecting the call sites.
    from fpulse.engine.step_output_store import StepOutputStore
    from fpulse.monitoring.store import ExecutionStore
    from fpulse.storage.database import Database
    from fpulse.storage.workflow_store import WorkflowStore


# ── Internal plumbing ────────────────────────────────────────────────────


def _state() -> dict[str, Any]:
    """Return the live ``app_state`` dict.

    Imported lazily so this module can be safely imported from
    ``fpulse.main`` itself without creating a circular dependency.
    """
    from fpulse.main import app_state  # local import — see docstring
    return app_state


def _require(key: str) -> Any:
    state = _state()
    value = state.get(key)
    if value is None:
        raise RuntimeError(
            f"app_state[{key!r}] is not initialized. The lifespan did not "
            f"populate it (or a test replaced app_state with a fresh dict "
            f"instead of mutating the real one via monkeypatch.setitem). "
            f"Check fpulse.main._populate_state and the test fixtures."
        )
    return value


# ── Required singletons (raise on missing) ───────────────────────────────


def get_db() -> "Database":
    """Return the SQLite ``Database`` populated at lifespan startup."""
    return _require("db")


def get_workflow_store() -> "WorkflowStore":
    """Return the global workflow store (key: ``store``).

    The key is historically called ``store`` (singular) because at one
    point it was the only store. New code should still use this accessor
    rather than the bare lookup so the name can be migrated to
    ``workflow_store`` later in a single place.
    """
    return _require("store")


def get_execution_store() -> "ExecutionStore":
    """Return the execution monitoring store."""
    return _require("execution_store")


def get_step_output_store() -> "StepOutputStore":
    """Return the step-IO capture store (Step IO replay viewer)."""
    return _require("step_output_store")


# ── Optional singletons (return None on missing) ─────────────────────────


def try_get_wallet_guard() -> Any | None:
    """Return the per-process WalletGuard, or None when not configured.

    Best-effort wiring (Step 1.5b-4) — agent endpoints degrade to no
    wallet enforcement when this is absent.
    """
    return _state().get("wallet_guard")


def try_get_trace_store() -> Any | None:
    """Return the agent trace store, or None when not configured."""
    return _state().get("trace_store")


def try_get_rag_embedder() -> Any | None:
    """Return the RAG embedder, or None when RAG isn't configured."""
    return _state().get("rag_embedder")


def try_get_rag_store() -> Any | None:
    """Return the RAG vector store, or None when RAG isn't configured."""
    return _state().get("rag_store")


def try_get_ai_config_store() -> Any | None:
    """Return the AI config store, or None when no AI provider is wired."""
    return _state().get("ai_config_store")


def try_get_encryptor() -> Any | None:
    """Return the credentials encryptor, or None when key wiring failed."""
    return _state().get("encryptor")


# ── Factory shim (for tests) ─────────────────────────────────────────────


def create_app(*, testing: bool = False) -> Any:
    """Return the FastAPI application.

    Today this is a thin shim over the module-level ``fpulse.main.app``
    instance — there is no real factory yet, because every router import
    runs at module load and the lifespan owns store wiring. Tests that
    want isolation should use ``TestClient(app)`` as a context manager
    (which triggers the lifespan) and reset shared resources via
    ``monkeypatch.setitem(app_state, ...)`` rather than rebinding the
    module attribute.

    The shim exists so tests can write ``from fpulse.state import
    create_app`` today, and the day this is converted into a real
    factory ``create_app(config)`` returning a fresh FastAPI instance,
    the test code doesn't change.
    """
    from fpulse.main import app  # local import to avoid circular deps
    if testing:
        # Future: a real factory would accept testing=True to skip lifespan
        # side effects (e.g. background warmup, scheduler start). Today the
        # flag is accepted but a no-op — tests still drive lifespan via
        # `with TestClient(app)`.
        pass
    return app
