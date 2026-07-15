"""Freshness contract enforcement.

A SourceProfile may declare `freshness_interval_seconds` — e.g. an
on-prem source whose backing collector only refreshes every 6 hours.
Polling the API more often than that produces no new data and stresses
the source for nothing.

The FreshnessGate reads the latest manifest for the profile and
decides whether enough time has elapsed since the last completed run.
The engine consults the gate at startup; the API exposes it so the
UI can show 'next refresh allowed at HH:MM' before the user even
attempts a run.

`force=True` always passes — operators can still kick off a manual
run for debugging, but the audit log shows it was an override.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from fpulse.extraction.manifest import RunManifest


class FreshnessBlocked(RuntimeError):
    """Raised by the engine when the gate refuses a run."""

    def __init__(self, decision: "FreshnessDecision") -> None:
        super().__init__(decision.reason)
        self.decision = decision


@dataclass
class FreshnessDecision:
    allowed: bool
    reason: str
    last_completed_at: float | None
    next_allowed_at: float | None
    forced: bool = False

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "last_completed_at": self.last_completed_at,
            "next_allowed_at": self.next_allowed_at,
            "forced": self.forced,
        }


class FreshnessGate:
    """Decides whether a fresh run of `profile` is allowed right now,
    based on the latest manifest in `manifest_dir`."""

    def __init__(self, manifest_dir: str) -> None:
        self.manifest_dir = manifest_dir

    def check(self, profile, *, force: bool = False) -> FreshnessDecision:  # type: SourceProfile
        if force:
            return FreshnessDecision(
                allowed=True,
                reason="force=True bypasses freshness gate",
                last_completed_at=None,
                next_allowed_at=None,
                forced=True,
            )

        interval = profile.freshness_interval_seconds
        if interval is None or interval <= 0:
            return FreshnessDecision(
                allowed=True,
                reason="profile has no freshness_interval_seconds",
                last_completed_at=None,
                next_allowed_at=None,
            )

        latest = RunManifest.latest(self.manifest_dir, profile.name)
        if latest is None or latest.completed_at is None:
            return FreshnessDecision(
                allowed=True,
                reason="no prior completed run for this profile",
                last_completed_at=None,
                next_allowed_at=None,
            )

        elapsed = time.time() - latest.completed_at
        if elapsed >= interval:
            return FreshnessDecision(
                allowed=True,
                reason=f"last run {int(elapsed)}s ago, interval {interval}s",
                last_completed_at=latest.completed_at,
                next_allowed_at=None,
            )

        next_allowed = latest.completed_at + interval
        return FreshnessDecision(
            allowed=False,
            reason=(
                f"freshness contract: profile declares {interval}s minimum interval; "
                f"last successful run was {int(elapsed)}s ago "
                f"(next allowed in {int(next_allowed - time.time())}s)"
            ),
            last_completed_at=latest.completed_at,
            next_allowed_at=next_allowed,
        )
