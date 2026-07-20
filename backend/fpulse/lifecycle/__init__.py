"""Pipeline Activate / Deactivate request store (PR12).

PROD activate / deactivate is approval-gated — instead of flipping
``workflow.is_active_prod`` directly, the user creates a
``lifecycle_toggle_requests`` row and an admin approves it. DEV path
is direct (no rows here, no approval).

Schema: see ``storage/database.py`` v21 migration for the
``lifecycle_toggle_requests`` table.
"""
from .store import LifecycleToggleRequest, LifecycleToggleStore

__all__ = ["LifecycleToggleRequest", "LifecycleToggleStore"]
