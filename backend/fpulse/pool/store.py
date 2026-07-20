"""PoolAllocationStore — SQLite operations on pool_allocations.

Schema is in ``storage/database.py`` (v22 migration). The CHECK
constraint guarantees the three percentages always sum to 100, so the
runtime never has to silently round.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ── Defaults — see DESIGN_PROD_SANDBOX-style discussion in PR14 chat ──
DEFAULT_PROD_PCT: int = 60
DEFAULT_DEV_PCT: int = 20
DEFAULT_BURST_PCT: int = 20


@dataclass
class PoolAllocation:
    workspace_id: str
    prod_reserved_pct: int = DEFAULT_PROD_PCT
    dev_reserved_pct: int = DEFAULT_DEV_PCT
    burst_pct: int = DEFAULT_BURST_PCT
    updated_at: str = ""
    updated_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "prod_reserved_pct": self.prod_reserved_pct,
            "dev_reserved_pct": self.dev_reserved_pct,
            "burst_pct": self.burst_pct,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    def slots(self, total_workers: int) -> dict[str, int]:
        """Translate percentages into integer worker counts.

        The three slices always sum to ``total_workers`` even after
        floor-rounding — any rounding remainder is added to the burst
        pool (the most flexible lane). This keeps the admit logic from
        having a "phantom worker" rounding bug.
        """
        prod = (total_workers * self.prod_reserved_pct) // 100
        dev = (total_workers * self.dev_reserved_pct) // 100
        burst = total_workers - prod - dev
        return {"prod": prod, "dev": dev, "burst": burst}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_alloc(row: sqlite3.Row | tuple, workspace_id: str | None = None) -> PoolAllocation:
    if isinstance(row, sqlite3.Row):
        d = dict(row)
    else:
        cols = ("workspace_id", "prod_reserved_pct", "dev_reserved_pct",
                "burst_pct", "updated_at", "updated_by")
        d = dict(zip(cols, row))
    return PoolAllocation(
        workspace_id=d.get("workspace_id") or workspace_id or "default",
        prod_reserved_pct=int(d.get("prod_reserved_pct") or DEFAULT_PROD_PCT),
        dev_reserved_pct=int(d.get("dev_reserved_pct") or DEFAULT_DEV_PCT),
        burst_pct=int(d.get("burst_pct") or DEFAULT_BURST_PCT),
        updated_at=d.get("updated_at") or "",
        updated_by=d.get("updated_by"),
    )


class PoolAllocationStore:
    """Thin SQLite repo for ``pool_allocations``.

    Stateless — caller supplies the connection per call.
    """

    def get(self, conn: sqlite3.Connection, workspace_id: str) -> PoolAllocation:
        """Return the workspace's allocation, or the 60/20/20 default
        wrapped in a synthetic PoolAllocation if no row exists yet.
        Default rows are NOT auto-created; use ``upsert`` to persist."""
        cur = conn.execute(
            "SELECT workspace_id, prod_reserved_pct, dev_reserved_pct, burst_pct, "
            "updated_at, updated_by FROM pool_allocations WHERE workspace_id = ?",
            (workspace_id,),
        )
        row = cur.fetchone()
        if row:
            return _row_to_alloc(row, workspace_id)
        return PoolAllocation(workspace_id=workspace_id)

    def upsert(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        prod_reserved_pct: int,
        dev_reserved_pct: int,
        burst_pct: int,
        updated_by: str,
    ) -> PoolAllocation:
        """Persist a new allocation. The DB CHECK constraint will reject
        any combination that doesn't sum to 100; surface that as a
        ValueError so callers can return a clean 400 instead of 500.
        """
        if prod_reserved_pct + dev_reserved_pct + burst_pct != 100:
            raise ValueError(
                f"Pool percentages must sum to 100 "
                f"(got prod={prod_reserved_pct} + dev={dev_reserved_pct} + "
                f"burst={burst_pct} = {prod_reserved_pct + dev_reserved_pct + burst_pct})"
            )
        for name, val in [
            ("prod_reserved_pct", prod_reserved_pct),
            ("dev_reserved_pct", dev_reserved_pct),
            ("burst_pct", burst_pct),
        ]:
            if val < 0 or val > 100:
                raise ValueError(f"{name} must be in [0, 100], got {val}")

        now = _now_iso()
        conn.execute(
            """
            INSERT INTO pool_allocations (
                workspace_id, prod_reserved_pct, dev_reserved_pct,
                burst_pct, updated_at, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                prod_reserved_pct = excluded.prod_reserved_pct,
                dev_reserved_pct  = excluded.dev_reserved_pct,
                burst_pct         = excluded.burst_pct,
                updated_at        = excluded.updated_at,
                updated_by        = excluded.updated_by
            """,
            (
                workspace_id, prod_reserved_pct, dev_reserved_pct,
                burst_pct, now, updated_by,
            ),
        )
        conn.commit()
        return PoolAllocation(
            workspace_id=workspace_id,
            prod_reserved_pct=prod_reserved_pct,
            dev_reserved_pct=dev_reserved_pct,
            burst_pct=burst_pct,
            updated_at=now,
            updated_by=updated_by,
        )


# ─────────────────────────────────────────────────────────────────────────
# Admit-time lane assignment
# ─────────────────────────────────────────────────────────────────────────

def pick_lane(
    env: str,
    busy_per_lane: dict[str, int],
    slots: dict[str, int],
) -> str | None:
    """Return the lane name (``'prod'`` / ``'dev'`` / ``'burst'``) that
    a new task tagged with ``env`` should consume a slot from, or
    ``None`` if every eligible lane is at capacity.

    Rules:
    * A PROD task prefers its **reserved** lane; falls back to **burst**
      when reserved is full.
    * A DEV task prefers its **reserved** lane; falls back to **burst**.
    * A task NEVER takes a slot from the OTHER env's reserved lane.
      That's the whole point of the reservation — guarantees PROD a
      floor of capacity even when DEV is on a tear.

    ``busy_per_lane`` maps lane name → currently-busy count.
    ``slots``        maps lane name → capacity (from PoolAllocation.slots(N)).
    """
    env = (env or "dev").lower()
    if env not in ("dev", "prod"):
        env = "dev"

    # Try the reserved lane for this env.
    if busy_per_lane.get(env, 0) < slots.get(env, 0):
        return env
    # Spill into burst if it has capacity.
    if busy_per_lane.get("burst", 0) < slots.get("burst", 0):
        return "burst"
    # Everything full.
    return None
