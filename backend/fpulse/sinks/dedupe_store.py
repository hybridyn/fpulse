"""
IdempotencyDedupeStore — per-(pipeline, sink_step) hash index for
external-sink side-effect dedup.

Why this exists
───────────────
External sinks (email_sink, webhook_sink, api_sink, kafka_sink,
slack_notify) fire real-world side effects per row. The frontend
idempotency classifier already marks these red and the backfill flow
refuses to auto-replay them, but a user who acknowledges the warning,
re-runs after a partial failure, or replays a window manually can still
trigger duplicate fires: the second pass has no memory of what the
first pass already sent.

This store closes that gap. When an external sink has an
``idempotency_key`` template configured, the helper in
``fpulse.sinks.idempotency_helper`` renders the template per row, hashes
the result with sha256, and asks this store "have we already sent for
(pipeline_id, sink_step_id, key_hash)?". The store answers True/False
and, on a fresh fire, records the hash with a TTL.

Design notes
────────────
* Storage is the operator's existing ``fpulse.db`` SQLite file — same
  WAL settings, same backup story. No new files, no new processes.

* The table is scoped by ``(pipeline_id, sink_step_id, key_hash)`` so:
    - the same row hash sent by two *different* sinks doesn't collide
      (e.g. an email and a webhook can both send to the same recipient
      without one cancelling the other);
    - the same row hash sent from a *different pipeline* doesn't
      collide (a "send welcome email" pipeline and an "audit log"
      pipeline that happen to key on the same user_id stay
      independent).

* TTL defaults to 30 days. This is a conscious tradeoff:
    - Too short: a monthly report sink would re-send rows it already
      sent last month.
    - Too long: the table grows unbounded for high-volume sinks.
  30 days covers the common monthly / weekly / daily cadences while
  still pruning lazily. Operators with a stronger need can pass a
  custom TTL per record() call (the sink param is
  ``idempotency_ttl_days``).

* Pruning is *lazy* on lookup. If ``seen()`` finds a hit whose
  ``expires_at`` is in the past, it returns False (treat as not-seen)
  and lets ``record()`` re-insert. We do NOT delete the row on the
  fly — that would mean a SELECT followed by a DELETE under load,
  which adds a write per false-positive. A future background sweeper
  can do the deletes in bulk; the on-demand path stays cheap.

* The store is a *module-level singleton*. ``fpulse.main`` is expected
  to call ``get_dedupe_store().set_db(db)`` at startup so the executor
  doesn't have to thread the dict around. Tests build a fresh
  ``IdempotencyDedupeStore(db=test_db)`` directly. When ``set_db`` has
  never been called, every method is a defensive no-op (returns
  not-seen, swallows record()) so a missing wiring never breaks a sink.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Default TTL: 30 days. Picked to comfortably cover the most common
# user cadences (daily / weekly / monthly recurring sinks) without
# growing the table indefinitely for high-volume per-row sinks. The
# `record()` call accepts an override so per-sink tuning is one
# parameter away.
DEFAULT_TTL_SECONDS = 86400 * 30


class IdempotencyDedupeStore:
    """SQLite-backed (pipeline, sink, hash) → seen-marker index.

    All operations are best-effort: a failure to read or write the
    dedup record NEVER fails the pipeline run. We log and degrade to
    "treat as not-seen" so the sink still fires (the worst case is a
    duplicate send the next run, which is what idempotency keys are
    designed to *prevent* — but a hard failure of the dedup store
    must never *cause* a duplicate either).

    Thread-safe via the underlying ``Database`` (per-thread connection,
    WAL).
    """

    def __init__(self, db=None):
        self._db = db

    def set_db(self, db) -> None:
        """Wire up the SQLite ``Database`` handle. Called by main.py at
        startup; tests pass the per-test DB directly to the constructor.
        """
        self._db = db

    # ── Lookup ───────────────────────────────────────────────────────

    def seen(self, pipeline_id: str, sink_step_id: str, key_hash: str) -> bool:
        """True if we've already recorded this (pipeline, sink, hash).

        Lazy TTL handling: if the row exists but its ``expires_at`` is
        in the past, we return False so the caller fires the sink and
        a fresh ``record()`` resets the marker. We do NOT delete the
        stale row here — a background sweeper (future work) can prune
        in bulk; the hot path stays read-only.

        Returns False on any storage error (defensive: a broken dedup
        store should never block a sink from firing — duplicates are
        the failure mode we tolerate, missed sends are not).
        """
        if self._db is None:
            return False
        if not (pipeline_id and sink_step_id and key_hash):
            # An empty key never gets recorded, so "seen" is trivially
            # False. The helper module enforces this at the caller side
            # too, but defending here keeps the store usable in
            # isolation.
            return False
        try:
            row = self._db.fetchone(
                "SELECT expires_at FROM sink_idempotency "
                "WHERE pipeline_id = ? AND sink_step_id = ? AND key_hash = ?",
                (pipeline_id, sink_step_id, key_hash),
            )
            if not row:
                return False
            expires_at = row.get("expires_at")
            if not expires_at:
                # No TTL recorded — treat as still valid (legacy rows,
                # or callers that explicitly asked for no expiry).
                return True
            return not _is_past(expires_at)
        except Exception as exc:  # noqa: BLE001 — never fail the sink
            logger.warning(
                "IdempotencyDedupeStore.seen failed for pipeline=%s sink=%s: %s",
                pipeline_id, sink_step_id, exc,
            )
            return False

    # ── Record ───────────────────────────────────────────────────────

    def record(
        self,
        pipeline_id: str,
        sink_step_id: str,
        key_hash: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        """Mark (pipeline, sink, hash) as sent.

        Uses INSERT OR REPLACE so a stale (expired) row is overwritten
        with the new TTL on the next fire. The UNIQUE index on
        (pipeline_id, sink_step_id, key_hash) is what makes the
        REPLACE safe — without it, every re-fire would append a fresh
        row and the table would grow with every retry.

        Best-effort: a failure to write the marker logs and returns.
        The next run will see the row as not-seen and may send a
        duplicate — but the alternative (failing the sink because the
        marker write failed) is worse: every retry would also fail to
        write the marker, so the user can't make forward progress.
        """
        if self._db is None:
            return
        if not (pipeline_id and sink_step_id and key_hash):
            return
        try:
            now = datetime.now(timezone.utc)
            expires_at = (
                now + timedelta(seconds=int(ttl_seconds))
                if ttl_seconds and ttl_seconds > 0
                else None
            )
            self._db.execute(
                "INSERT OR REPLACE INTO sink_idempotency "
                "(pipeline_id, sink_step_id, key_hash, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    pipeline_id,
                    sink_step_id,
                    key_hash,
                    now.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                ),
            )
            self._db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "IdempotencyDedupeStore.record failed for pipeline=%s sink=%s: %s",
                pipeline_id, sink_step_id, exc,
            )

    # ── Stats ────────────────────────────────────────────────────────

    def stats(self, pipeline_id: str) -> dict[str, Any]:
        """Return counts for a pipeline's dedup index.

        Shape:
            {
                "total": int,             # rows for this pipeline
                "last_24h": int,          # rows created in the last 24 hours
                "oldest_age_seconds": int # age of the oldest row (0 if empty)
            }

        Useful for the Storage / Diagnostics page once it grows a
        "dedup index" panel. Always returns valid integers — a
        storage error logs and falls back to zeros so a UI render
        never sees None.
        """
        empty: dict[str, Any] = {
            "total": 0, "last_24h": 0, "oldest_age_seconds": 0,
        }
        if self._db is None or not pipeline_id:
            return empty
        try:
            total_row = self._db.fetchone(
                "SELECT COUNT(*) AS n FROM sink_idempotency "
                "WHERE pipeline_id = ?",
                (pipeline_id,),
            )
            total = int((total_row or {}).get("n") or 0)

            since = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat()
            last_24h_row = self._db.fetchone(
                "SELECT COUNT(*) AS n FROM sink_idempotency "
                "WHERE pipeline_id = ? AND created_at >= ?",
                (pipeline_id, since),
            )
            last_24h = int((last_24h_row or {}).get("n") or 0)

            oldest_row = self._db.fetchone(
                "SELECT MIN(created_at) AS oldest FROM sink_idempotency "
                "WHERE pipeline_id = ?",
                (pipeline_id,),
            )
            oldest = (oldest_row or {}).get("oldest")
            oldest_age = _age_seconds(oldest) if oldest else 0

            return {
                "total": total,
                "last_24h": last_24h,
                "oldest_age_seconds": oldest_age,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "IdempotencyDedupeStore.stats failed for pipeline=%s: %s",
                pipeline_id, exc,
            )
            return empty


# ── Helpers ──────────────────────────────────────────────────────────


def _is_past(iso_ts: str) -> bool:
    """True if the ISO 8601 timestamp is in the past (UTC)."""
    try:
        ts = datetime.fromisoformat(iso_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        # An unparseable timestamp is treated as still-valid so the
        # caller doesn't accidentally re-fire a sink because of a
        # storage glitch.
        return False


def _age_seconds(iso_ts: str) -> int:
    """Return seconds since the ISO 8601 timestamp, or 0 on error."""
    try:
        ts = datetime.fromisoformat(iso_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return max(int(delta.total_seconds()), 0)
    except (TypeError, ValueError):
        return 0


# ── Module-level singleton ───────────────────────────────────────────
#
# Same wiring pattern as ``fpulse.engine.checkpoint_store``:
# ``fpulse.main`` calls ``get_dedupe_store().set_db(db)`` at startup so
# the sinks (and any other consumer) can grab a working store via a
# plain function call. Test code constructs a fresh
# ``IdempotencyDedupeStore(db=test_db)`` directly to keep test
# isolation.

_dedupe_store = IdempotencyDedupeStore()


def get_dedupe_store() -> IdempotencyDedupeStore:
    """Return the process-wide ``IdempotencyDedupeStore`` singleton."""
    return _dedupe_store
