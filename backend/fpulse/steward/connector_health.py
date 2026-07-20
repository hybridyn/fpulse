"""F-Pulse Steward - connector-health detector (2026-06-07).

The first CONNECTOR-level Steward detector. Activates four FindingKinds
that had been contract-only in 1.1:

  * CONNECTOR_AUTH_FAILURE   - auth/credential problems
  * CONNECTOR_UNREACHABLE    - network / DNS / connection-refused
  * CONNECTOR_RATE_LIMIT     - 429s / throttling errors
  * CREDENTIAL_NEAR_EXPIRY   - cert / token / key expires in N days

# Design

Per-workspace health state is recorded as a single JSON file at
``<data_dir>/steward/<ws>/connector_health.json``. The state tracks
what the Connection table can't track on its own:

  * ``consecutive_failures``  - how many tests have failed in a row
  * ``first_failure_at``      - when the current failure streak started
                                (used for the time-clamped escalation
                                Rule 6 - we don't crank up severity on
                                a 30-second flap)
  * ``last_status``           - healthy | failing | unknown
  * ``last_error_class``      - normalised error category (see
                                classify_error) so the detector picks
                                the right FindingKind

# Recording paths

Two ways health updates reach the store:

  1. **Built-in connection-test endpoint** (api/connections.py) calls
     ``record_test_outcome()`` after every Test click. This is the
     primary path - any user who tests a connection updates Steward.

  2. **External POST** (``POST /api/steward/connector-health``) lets
     CI runners / external monitoring tools push health updates without
     needing F-Pulse to be the one running the probe. Useful for users
     who run health checks from outside the F-Pulse process.

# Detector rules

A finding is emitted when ALL hold:
  * ``consecutive_failures >= 2`` (single-flap suppression)
  * ``first_failure_at`` was at least 5 minutes ago
    (time-clamp - Rule 6 about historical baseline variance)
  * The connection signature isn't in the workspace suppression set

Severity scales with consecutive_failures:
  *  2-3   -> P3
  *  4-9   -> P2
  *  10+   -> P1

Read-only: this module never mutates the Connection record itself.
The existing connections.py already persists ``last_test_ok`` /
``last_test_error`` on the Connection; Steward's sidecar just tracks
the additional metrics (streak counter, first-failure timestamp,
classified error) without duplicating data the Connection already
holds.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)


_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    """ISO-8601 parser tolerant of trailing Z (Python <3.11 didn't
    accept it on fromisoformat; we keep the tolerant version because
    health-state files persist across upgrades)."""
    if not value:
        return None
    try:
        v = value.rstrip("Z")
        if "T" in v and "+" not in v and "-" not in v.split("T", 1)[1]:
            v += "+00:00"
        return datetime.fromisoformat(v)
    except Exception:
        return None


# ── Error classification ─────────────────────────────────────────────


# Substring lists rather than regex with \b boundaries. Word boundaries
# rejected prefix matches like "auth" in "Authentication" - and the
# false-positive risk (matching "auth" in unrelated words) is negligible
# in driver-error strings, which almost never reference authority /
# author / etc. unrelated to authentication.
#
# Order matters: auth checked before rate-limit so "401 Unauthorized"
# classifies as auth, not as a 4xx rate-limit pattern. Each list reads
# top-down for the first match.
_AUTH_KEYWORDS = (
    "auth", "credential", "permission", "password", "unauthor",
    "401", "403", "access denied", "invalid token", "forbidden",
)
_RATE_LIMIT_KEYWORDS = (
    "rate limit", "rate-limit", "ratelimit", "429", "throttl",
    "too many requests", "quota exceeded",
)
_TIMEOUT_KEYWORDS = (
    "timeout", "time out", "timed out", "deadline exceeded",
)
_UNREACHABLE_KEYWORDS = (
    "connection refused", "unreachable", "could not connect",
    "name or service not known", "getaddrinfo", "dns",
    "host not found", "no route to host", "network is unreachable",
    "ssl",
)


def classify_error(error_text: str | None) -> str:
    """Map a free-text test error to one of {auth_error, rate_limit,
    timeout, unreachable, unknown}. Used to pick the right FindingKind
    when a connection's test fails."""
    if not error_text:
        return "unknown"
    low = error_text.lower()
    if any(kw in low for kw in _AUTH_KEYWORDS):
        return "auth_error"
    if any(kw in low for kw in _RATE_LIMIT_KEYWORDS):
        return "rate_limit"
    if any(kw in low for kw in _TIMEOUT_KEYWORDS):
        return "timeout"
    if any(kw in low for kw in _UNREACHABLE_KEYWORDS):
        return "unreachable"
    return "unknown"


# ── State model ──────────────────────────────────────────────────────


class ConnectorHealthState(BaseModel):
    """Per-connection health record.

    Holds the streak + classification data that the Connection table
    doesn't track on its own. Reset to a healthy state on the first
    successful test after a failure streak."""

    connection_id: str
    consecutive_failures: int = 0
    first_failure_at: str | None = None
    last_check_at: str | None = None
    last_status: str = "unknown"  # healthy | failing | unknown
    last_error_class: str = "unknown"  # see classify_error()
    last_error_message: str = ""
    latency_ms: int | None = None
    # Optional credential-expiry hint - written by callers that know
    # when the underlying credential expires (cert / token / API key).
    # detect_connector_health emits CREDENTIAL_NEAR_EXPIRY when this
    # is within the warning window.
    credential_expires_at: str | None = None


# ── Store ────────────────────────────────────────────────────────────


class ConnectorHealthStore:
    """File-backed dict store at
    ``<data_dir>/steward/<ws>/connector_health.json``.

    Single file per workspace - simpler than per-connection sidecars
    for a few hundred connections, and the per-call cost is dominated
    by the lock anyway."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
            if not isinstance(raw, dict):
                return {}
            return raw
        except Exception:
            # Corrupt file - return empty rather than crashing the
            # scan. Same resilience pattern as memory.py.
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with _FILE_LOCK:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(data, fp, indent=2, ensure_ascii=False)
            tmp.replace(self.path)

    def get(self, connection_id: str) -> ConnectorHealthState | None:
        data = self._load()
        entry = data.get(connection_id)
        if entry is None:
            return None
        try:
            return ConnectorHealthState.model_validate(entry)
        except Exception:
            return None

    def upsert(self, state: ConnectorHealthState) -> None:
        data = self._load()
        data[state.connection_id] = state.model_dump(mode="json")
        self._save(data)

    def all(self) -> list[ConnectorHealthState]:
        out: list[ConnectorHealthState] = []
        for raw in self._load().values():
            try:
                out.append(ConnectorHealthState.model_validate(raw))
            except Exception:
                continue
        return out


# ── Recorder ─────────────────────────────────────────────────────────


def record_test_outcome(
    store: ConnectorHealthStore,
    *,
    connection_id: str,
    ok: bool,
    error_message: str = "",
    latency_ms: int | None = None,
    credential_expires_at: str | None = None,
) -> ConnectorHealthState:
    """Update the health record after a connection test.

    On success: resets consecutive_failures and first_failure_at, sets
    status=healthy. On failure: increments the streak; stamps
    first_failure_at if this is the first failure of the current streak.

    Safe to call from any code path - the existing test_connection
    endpoint, an external POST, a scheduled health probe, etc."""
    existing = store.get(connection_id) or ConnectorHealthState(connection_id=connection_id)
    now = _iso_now()

    if ok:
        new_state = ConnectorHealthState(
            connection_id=connection_id,
            consecutive_failures=0,
            first_failure_at=None,
            last_check_at=now,
            last_status="healthy",
            last_error_class="unknown",
            last_error_message="",
            latency_ms=latency_ms,
            credential_expires_at=credential_expires_at or existing.credential_expires_at,
        )
    else:
        streak = existing.consecutive_failures + 1 if existing.last_status == "failing" else 1
        first_failure = existing.first_failure_at if (streak > 1 and existing.first_failure_at) else now
        new_state = ConnectorHealthState(
            connection_id=connection_id,
            consecutive_failures=streak,
            first_failure_at=first_failure,
            last_check_at=now,
            last_status="failing",
            last_error_class=classify_error(error_message),
            last_error_message=(error_message or "")[:500],
            latency_ms=latency_ms,
            credential_expires_at=credential_expires_at or existing.credential_expires_at,
        )

    store.upsert(new_state)
    return new_state


# ── Detector ─────────────────────────────────────────────────────────


# Streak thresholds for the time-clamped escalation (per Rule 6 -
# Historical Baseline Variance). A connection that flapped once doesn't
# get flagged; a sustained failure does.
_MIN_STREAK_TO_EMIT = 2
_MIN_MINUTES_SINCE_FIRST_FAILURE = 5

# Credential-expiry warning window - emits CREDENTIAL_NEAR_EXPIRY when
# the recorded expiry is within this many days from now.
_CREDENTIAL_EXPIRY_WARNING_DAYS = 7


def _severity_for_streak(streak: int) -> FindingSeverity:
    if streak >= 10:
        return FindingSeverity.P1
    if streak >= 4:
        return FindingSeverity.P2
    return FindingSeverity.P3


def _kind_for_error_class(error_class: str) -> FindingKind:
    return {
        "auth_error":   FindingKind.CONNECTOR_AUTH_FAILURE,
        "rate_limit":   FindingKind.CONNECTOR_RATE_LIMIT,
        "unreachable":  FindingKind.CONNECTOR_UNREACHABLE,
        "timeout":      FindingKind.CONNECTOR_UNREACHABLE,  # collapses to unreachable
    }.get(error_class, FindingKind.CONNECTOR_UNREACHABLE)


def _connection_signature(workspace_id: str, connection_id: str, kind: FindingKind) -> str:
    """Per (workspace, connection, kind) signature so a user can dismiss
    ONE class of failure on a connection without silencing the other
    classes (e.g. dismiss "rate-limit alerts are expected here" but
    keep auth-failure alerting live)."""
    raw = f"connhealth::{workspace_id}::{connection_id}::{kind.value}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _finding_id(prefix: str, signature: str) -> str:
    return f"{prefix}-{signature[:12]}"


def detect_connector_health(
    connections: list[dict[str, Any]],
    health_store: ConnectorHealthStore,
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
) -> list[StewardFinding]:
    """Emit findings for connections in sustained bad state, plus any
    with credentials nearing expiry.

    ``connections`` are dicts shaped like the connection model dump
    (id, name, type, last_test_ok, last_test_at, last_test_error).
    They feed the recording side; the health_store feeds the detection
    side. Both must agree before a finding emits.
    """
    suppressed = suppressed_signatures or set()
    findings: list[StewardFinding] = []
    now = _iso_now()
    now_dt = datetime.now(timezone.utc)

    # Build a quick lookup of connection details for evidence.
    conn_by_id: dict[str, dict[str, Any]] = {}
    for c in connections:
        if isinstance(c, dict):
            cid = str(c.get("id") or "")
            if cid:
                conn_by_id[cid] = c

    for state in sorted(health_store.all(), key=lambda s: s.connection_id):
        conn = conn_by_id.get(state.connection_id, {})
        conn_name = str(conn.get("name") or state.connection_id)
        conn_type = str(conn.get("type") or "unknown")

        # ── Failure-streak finding ───────────────────────────────────
        if state.last_status == "failing" and state.consecutive_failures >= _MIN_STREAK_TO_EMIT:
            first_failure_dt = _parse_iso(state.first_failure_at)
            min_elapsed = first_failure_dt is None or (
                (now_dt - first_failure_dt) >= timedelta(minutes=_MIN_MINUTES_SINCE_FIRST_FAILURE)
            )
            if min_elapsed:
                kind = _kind_for_error_class(state.last_error_class)
                sig = _connection_signature(workspace_id, state.connection_id, kind)
                if sig not in suppressed:
                    severity = _severity_for_streak(state.consecutive_failures)
                    findings.append(StewardFinding(
                        id=_finding_id("conn", sig),
                        workspace_id=workspace_id,
                        kind=kind,
                        level=FindingLevel.CONNECTOR,
                        severity=severity,
                        status=FindingStatus.OPEN,
                        title=_title_for_kind(kind, conn_name, state.consecutive_failures),
                        body=_body_for_failure(state, conn_name, conn_type),
                        evidence={
                            "connection_id": state.connection_id,
                            "connection_name": conn_name,
                            "connection_type": conn_type,
                            "consecutive_failures": state.consecutive_failures,
                            "first_failure_at": state.first_failure_at,
                            "last_check_at": state.last_check_at,
                            "last_error_class": state.last_error_class,
                            "last_error_message": state.last_error_message,
                            "source_signature": sig,
                        },
                        proposed_actions=[
                            {
                                "label": "Dismiss (intentional / known-broken)",
                                "action": "suppress_finding",
                                "params": {"finding_id": _finding_id("conn", sig), "scope": "signature"},
                            },
                        ],
                        first_seen=state.first_failure_at or now,
                        last_seen=state.last_check_at or now,
                        occurrences=state.consecutive_failures,
                        confidence="high",
                        confidence_score=1.0,
                        evidence_count=state.consecutive_failures,
                        baseline_window="consecutive_test_failures",
                    ))

        # ── Credential-expiry finding ────────────────────────────────
        if state.credential_expires_at:
            expires_dt = _parse_iso(state.credential_expires_at)
            if expires_dt and expires_dt > now_dt:
                days_until = (expires_dt - now_dt).days
                if days_until <= _CREDENTIAL_EXPIRY_WARNING_DAYS:
                    kind = FindingKind.CREDENTIAL_NEAR_EXPIRY
                    sig = _connection_signature(workspace_id, state.connection_id, kind)
                    if sig not in suppressed:
                        severity = FindingSeverity.P1 if days_until <= 1 else (
                            FindingSeverity.P2 if days_until <= 3 else FindingSeverity.P3
                        )
                        findings.append(StewardFinding(
                            id=_finding_id("crexp", sig),
                            workspace_id=workspace_id,
                            kind=kind,
                            level=FindingLevel.CONNECTOR,
                            severity=severity,
                            status=FindingStatus.OPEN,
                            title=f"Credential expires in {days_until} day{'s' if days_until != 1 else ''}: {conn_name}",
                            body=(
                                f"The credential backing connection **{conn_name}** "
                                f"({conn_type}) is recorded as expiring at "
                                f"`{state.credential_expires_at}` - that's in {days_until} day"
                                f"{'s' if days_until != 1 else ''} from now. Rotate before "
                                f"expiry to avoid pipeline failures."
                            ),
                            evidence={
                                "connection_id": state.connection_id,
                                "connection_name": conn_name,
                                "connection_type": conn_type,
                                "credential_expires_at": state.credential_expires_at,
                                "days_until_expiry": days_until,
                                "source_signature": sig,
                            },
                            proposed_actions=[
                                {
                                    "label": "Dismiss (already scheduled rotation)",
                                    "action": "suppress_finding",
                                    "params": {"finding_id": _finding_id("crexp", sig), "scope": "signature"},
                                },
                            ],
                            first_seen=now,
                            last_seen=now,
                            occurrences=1,
                            confidence="high",
                            confidence_score=1.0,
                            evidence_count=1,
                            baseline_window=f"{_CREDENTIAL_EXPIRY_WARNING_DAYS}_day_window",
                        ))

    return findings


# ── Human-readable title / body builders ─────────────────────────────


def _title_for_kind(kind: FindingKind, conn_name: str, streak: int) -> str:
    pretty = {
        FindingKind.CONNECTOR_AUTH_FAILURE: "Auth failing",
        FindingKind.CONNECTOR_RATE_LIMIT:   "Rate-limited",
        FindingKind.CONNECTOR_UNREACHABLE:  "Unreachable",
    }.get(kind, "Connector failing")
    return f"{pretty} for {streak} consecutive checks: {conn_name}"


def _body_for_failure(state: ConnectorHealthState, conn_name: str, conn_type: str) -> str:
    pretty = {
        "auth_error":   "**Authentication is failing.** Credentials may be revoked, rotated, or wrong-scoped.",
        "rate_limit":   "**The source is rate-limiting us.** Either reduce request frequency or request a quota increase.",
        "unreachable":  "**Connection refused / network unreachable.** Network policy, DNS, or the source itself may be down.",
        "timeout":      "**Connection is timing out.** Network latency or source overload.",
    }.get(state.last_error_class, "**Connection test is failing.**")
    return (
        f"{pretty}\n\n"
        f"Connection **{conn_name}** ({conn_type}) has failed "
        f"**{state.consecutive_failures} consecutive** health checks "
        f"since `{state.first_failure_at}`.\n\n"
        + (f"Latest error:\n```\n{state.last_error_message}\n```\n" if state.last_error_message else "")
        + "Dismiss if this connection is intentionally down or under maintenance."
    )
