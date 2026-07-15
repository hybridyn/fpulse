"""Worker pool allocation (PR14).

Per-workspace logical split of the total worker fleet between PROD reserved,
DEV reserved, and a shared burst lane. Default 60/20/20.

Public surface:

* :class:`PoolAllocation`        — dataclass mirror of the SQLite row
* :class:`PoolAllocationStore`   — get/upsert helpers
* :func:`pick_lane`              — admit-time lane assignment for ExecutionManager
"""
from .store import (
    DEFAULT_BURST_PCT,
    DEFAULT_DEV_PCT,
    DEFAULT_PROD_PCT,
    PoolAllocation,
    PoolAllocationStore,
    pick_lane,
)

__all__ = [
    "DEFAULT_BURST_PCT",
    "DEFAULT_DEV_PCT",
    "DEFAULT_PROD_PCT",
    "PoolAllocation",
    "PoolAllocationStore",
    "pick_lane",
]
