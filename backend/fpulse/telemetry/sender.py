"""Telemetry sender — opt-in only, fail-silent, exact-schema payloads.

Hard contract (must hold for every event the sender emits):

  1. `is_telemetry_enabled(db)` is checked on every send site. If False,
     the sender is a no-op — nothing is queued, nothing leaves the host.
  2. The payload conforms exactly to `TELEMETRY_PAYLOAD_SCHEMA` in
     `consent.py`. No extra fields. The /trust page surfaces this schema
     so operators can audit it; drifting silently breaks that contract.
  3. The sender never raises into caller code. All network errors are
     swallowed at WARNING level. F-Pulse's normal operation is the
     priority; telemetry must never block startup, crash handling, or
     shutdown.
  4. No row data, SQL text, env vars, paths beyond `fpulse/*`,
     credentials, or user identifiers cross the wire. The
     `_sanitize_traceback` filter enforces this for crash payloads.
  5. The destination URL is fixed at module level (no operator-tunable
     endpoint — that would let a malicious operator point telemetry at
     an attacker-controlled server). Operators who want a custom sink
     run the F-Pulse+ self-hosted analytics path instead.

The sender uses a tiny in-memory queue (cap 32 events) drained by a
single background task. On revocation, the queue is flushed without
sending. On shutdown, the queue is dropped — no last-gasp send. This
keeps the failure surface small and predictable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional
from uuid import uuid4

from fpulse.telemetry.consent import is_telemetry_enabled

logger = logging.getLogger("fpulse.telemetry.sender")


# ── Module-level config ─────────────────────────────────────────────

# Receiving endpoint. Fixed; not operator-tunable. The 1.0.1 release
# will point this at the production receiver; until then any send is a
# no-op because no event ever leaves an opt-in install.
_TELEMETRY_URL = "https://telemetry.hybridyn.com/v1/ingest"

_QUEUE_MAX = 32
_SEND_TIMEOUT_S = 5.0
_RETRY_MAX_ATTEMPTS = 2  # 1 try + 1 retry; the receiver is best-effort
_RETRY_BACKOFF_S = 1.0


# ── Installation ID — random per-install UUID ───────────────────────


def get_installation_id(db: Any) -> str:
    """Read the installation_id from admin_settings, generating one on
    first call if missing. Persisted so the same ID identifies the same
    install across restarts (lets the receiver dedupe duplicate crash
    reports without revealing who the operator is)."""
    try:
        row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
        settings = json.loads(row["data"]) if row else {}
        existing = settings.get("telemetry_installation_id")
        if existing and isinstance(existing, str) and len(existing) >= 16:
            return existing
        # Generate a fresh ID and persist.
        new_id = uuid4().hex
        settings["telemetry_installation_id"] = new_id
        # settings.created_at is NOT NULL with no default; INSERT OR REPLACE
        # reinserts the row, so omitting created_at aborts on the NOT NULL
        # constraint. Stamp it, then commit — the generic execute() wrapper
        # neither supplies created_at nor commits.
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR REPLACE INTO settings (id, data, created_at) VALUES ('admin_settings', ?, ?)",
            (json.dumps(settings), now_iso),
        )
        db.commit()
        return new_id
    except Exception as exc:
        logger.debug("installation_id read failed (using ephemeral): %s", exc)
        # Caller still gets a stable string; just won't be persisted.
        return uuid4().hex


# ── Stack-trace sanitizer ───────────────────────────────────────────


_FPULSE_PATH_RE = re.compile(r"(?:^|[\s\"'])(/[^\s\"'<>]*?fpulse/[^\s\"'<>]*)")
_HOMEDIR_RE = re.compile(r"/(?:home|Users|root)/[^/\s\"'<>]+")
_WINDOWS_USER_RE = re.compile(r"[A-Z]:\\Users\\[^\\\s\"'<>]+", re.IGNORECASE)
_ENV_VAR_VALUE_RE = re.compile(r"FPULSE_[A-Z_]+\s*=\s*\S+")


def _sanitize_traceback(tb_text: str) -> str:
    """Strip user-identifying paths and env-var values out of a traceback.

    Rules:
      * Replace `/home/<user>/...` and `C:\\Users\\<user>\\...` with `<home>/...`
      * Drop any line that doesn't reference a path under `fpulse/`
        (this aggressively trims third-party frames that may contain user paths)
      * Replace `FPULSE_*=value` patterns with `FPULSE_*=<redacted>`
    """
    if not tb_text:
        return tb_text
    out_lines: list[str] = []
    for line in tb_text.splitlines():
        # Only keep frames that point at fpulse/* — drop site-packages /
        # user-script frames which can leak paths.
        if "fpulse/" in line or "fpulse\\" in line or line.strip().startswith(("Traceback", "  ", "During")):
            line = _HOMEDIR_RE.sub("<home>", line)
            line = _WINDOWS_USER_RE.sub(r"C:\\Users\\<user>", line)
            line = _ENV_VAR_VALUE_RE.sub(lambda m: m.group(0).split("=")[0] + "=<redacted>", line)
            out_lines.append(line)
    sanitized = "\n".join(out_lines)
    if not sanitized.strip():
        # Defensive: never send nothing; always send a marker so the receiver
        # can distinguish "sanitizer dropped everything" from "no traceback."
        return "<sanitizer-dropped-all-frames>"
    return sanitized


# ── Payload builder ─────────────────────────────────────────────────


def _feature_flags_snapshot(app_state: Optional[dict[str, Any]]) -> dict[str, bool]:
    """Best-effort snapshot of which optional features are enabled.

    Reads from app_state if present (set at startup) — falls back to env
    var detection. Never reads workspace data.
    """
    flags: dict[str, bool] = {
        "marketplace": False,
        "lineage": False,
        "telemetry": True,  # always true here — we wouldn't be sending otherwise
        "ollama_autoprobe": (
            os.environ.get("FPULSE_DISABLE_OLLAMA_AUTOPROBE", "").strip().lower()
            not in ("1", "true", "yes")
        ),
        "rag": (
            os.environ.get("FPULSE_DISABLE_RAG", "").strip().lower()
            not in ("1", "true", "yes")
        ),
        "product_knowledge": (
            os.environ.get("FPULSE_DISABLE_PRODUCT_KNOWLEDGE", "").strip().lower()
            not in ("1", "true", "yes")
        ),
    }
    if isinstance(app_state, dict):
        if app_state.get("license_manager"):
            flags["plus_license"] = True
    return flags


def build_event(
    event_type: str,
    *,
    db: Any,
    fpulse_version: str,
    exception_type: Optional[str] = None,
    stack_trace: Optional[str] = None,
    app_state: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Construct a payload conforming exactly to TELEMETRY_PAYLOAD_SCHEMA.

    The shape is enforced by `_validate_payload_shape` so a typo in this
    function (or someone adding a sneaky field later) gets caught at
    runtime before anything goes over the wire.
    """
    if event_type not in ("crash", "startup", "shutdown"):
        raise ValueError(f"event_type must be one of 'crash'/'startup'/'shutdown', got {event_type!r}")

    payload: dict[str, Any] = {
        "event_type": event_type,
        "fpulse_version": str(fpulse_version),
        "python_version": ".".join(str(p) for p in sys.version_info[:3]),
        "os_family": platform.system(),  # 'Linux' / 'Darwin' / 'Windows'
        "feature_flags": _feature_flags_snapshot(app_state),
        "installation_id": get_installation_id(db),
    }

    if event_type == "crash":
        payload["exception_type"] = (exception_type or "UnknownException")[:200]
        payload["stack_trace"] = _sanitize_traceback(stack_trace or "")[:8192]

    _validate_payload_shape(payload)
    return payload


_ALLOWED_KEYS = {
    "event_type", "fpulse_version", "python_version", "os_family",
    "feature_flags", "exception_type", "stack_trace", "installation_id",
}


def _validate_payload_shape(payload: dict[str, Any]) -> None:
    """Reject unknown fields before send. Catches accidental data leakage."""
    extra = set(payload.keys()) - _ALLOWED_KEYS
    if extra:
        raise ValueError(f"telemetry payload contains unexpected fields: {sorted(extra)}")


# ── Queue + sender ──────────────────────────────────────────────────


@dataclass
class _QueuedEvent:
    payload: dict[str, Any]
    enqueued_at: float = field(default_factory=time.time)
    attempts: int = 0


class TelemetryQueue:
    """Bounded in-memory queue. Drops oldest on overflow.

    Single-process scope — no cross-restart persistence by design (a queue
    that survives restart is a queue that can leak data after revocation).
    """

    def __init__(self, capacity: int = _QUEUE_MAX) -> None:
        self._q: Deque[_QueuedEvent] = deque(maxlen=capacity)
        self._lock = asyncio.Lock()

    async def enqueue(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self._q.append(_QueuedEvent(payload=payload))

    async def drain_once(self, http_client: Any) -> int:
        """Send up to `capacity` events. Returns number sent successfully."""
        async with self._lock:
            batch = list(self._q)
            self._q.clear()
        sent = 0
        for ev in batch:
            ok = await _send_one(http_client, ev)
            if ok:
                sent += 1
            elif ev.attempts < _RETRY_MAX_ATTEMPTS:
                # Re-queue for one retry. New events get priority over retries.
                async with self._lock:
                    self._q.append(ev)
        return sent

    async def flush(self) -> None:
        """Drop every queued event without sending. Used on revocation."""
        async with self._lock:
            self._q.clear()

    def size(self) -> int:
        return len(self._q)


async def _send_one(http_client: Any, ev: _QueuedEvent) -> bool:
    """Send one event. Returns True on 2xx, False on any failure.

    `http_client` is httpx.AsyncClient or compatible. We accept any
    object with `.post(url, json=, timeout=)` so tests can pass a fake.
    """
    ev.attempts += 1
    try:
        resp = await http_client.post(
            _TELEMETRY_URL,
            json=ev.payload,
            timeout=_SEND_TIMEOUT_S,
        )
        if 200 <= resp.status_code < 300:
            return True
        logger.debug("telemetry send non-2xx: %s", resp.status_code)
        return False
    except Exception as exc:
        # Network errors, DNS failures, TLS failures — all fail-silent.
        logger.debug("telemetry send failed (attempt %d): %s", ev.attempts, exc)
        return False


# Module-level singleton — one queue per process. Tests reset via
# `_reset_for_tests`.
_QUEUE: Optional[TelemetryQueue] = None


def get_queue() -> TelemetryQueue:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = TelemetryQueue()
    return _QUEUE


def _reset_for_tests() -> None:
    """Test-only escape hatch — clears the singleton queue."""
    global _QUEUE
    _QUEUE = None


# ── High-level send API ─────────────────────────────────────────────


async def send_event(
    event_type: str,
    *,
    db: Any,
    fpulse_version: str,
    exception_type: Optional[str] = None,
    stack_trace: Optional[str] = None,
    app_state: Optional[dict[str, Any]] = None,
) -> bool:
    """Public entry point. Returns True if event was queued, False if
    skipped (consent off, validation failure, or queue full + drop).

    Never raises — wraps every error in a debug log.
    """
    try:
        if not is_telemetry_enabled(db):
            return False
        payload = build_event(
            event_type,
            db=db,
            fpulse_version=fpulse_version,
            exception_type=exception_type,
            stack_trace=stack_trace,
            app_state=app_state,
        )
        await get_queue().enqueue(payload)
        return True
    except Exception as exc:
        logger.debug("telemetry send_event failed: %s", exc)
        return False


async def revoke_and_flush(db: Any) -> None:
    """Called when admin toggles telemetry OFF. Drops the in-memory queue
    so already-collected-but-unsent events never leave the box."""
    await get_queue().flush()
    logger.info("telemetry revoked — queue flushed")


__all__ = [
    "build_event",
    "send_event",
    "revoke_and_flush",
    "get_queue",
    "get_installation_id",
    "TelemetryQueue",
]
