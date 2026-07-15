"""DEPRECATED — removed in PR 5 (May 17 2026).

The built-in template gallery used to live here and was exposed via
`/api/templates` (GET / `/categories` / `/{id}` / `/{id}/use`). The OSS
frontend never consumed those endpoints; its built-in catalogue lives
in ``frontend/src/templates/catalog.ts``. Maintaining a second source of
truth here just caused silent drift when one was updated and the other
wasn't.

If you find yourself reaching for this file: don't. Add your templates
to ``frontend/src/templates/catalog.ts``. If you need a multi-tenant,
server-served gallery later, introduce a new module — don't resurrect
this one.
"""

from __future__ import annotations

# Intentionally empty — no callers should remain.
