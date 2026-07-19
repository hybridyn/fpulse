"""Per-workspace Steward configuration.

Lives at ``<data_dir>/steward/<workspace_id>/settings.json``. Plain
JSON so a user can edit by hand if the UI is offline.

Defaults are chosen to be **useful but not noisy**:
  * enabled = True (it's the headline feature; don't hide it by default)
  * min_severity = "p3" (show everything; user can dial it up if noisy)
  * scan_on_save = True (re-scan after every workflow save — sub-50ms,
    no executor impact, gives immediate feedback)
  * auto_stale_days = 30 (a finding the user never touches for 30 days
    auto-ages into 'stale' rather than nagging forever)
  * escalate_after_n_occurrences = 5 (the count at which an ignored
    finding gets bumped one severity step)
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field


_FILE_LOCK = threading.Lock()


class DetectorOverride(BaseModel):
    """Per-detector tuning (Coverage page, 2026-06-18 — rung 1).

    A detector with no entry here uses its built-in defaults (enabled, with
    the severity each finding ships with). An entry lets the operator make
    Coverage *their* policy: silence a detector that doesn't fit their
    workspace, or re-rank one to match their priorities.
    """

    enabled: bool = Field(
        default=True,
        description="When false, this detector's findings are dropped from "
                    "scans (and never escalate or notify). History is kept; "
                    "re-enabling resurfaces still-open findings.",
    )
    severity: Optional[Literal["p1", "p2", "p3"]] = Field(
        default=None,
        description="Override the severity of every finding from this "
                    "detector. None = keep the detector's built-in severity.",
    )
    thresholds: dict[str, float] = Field(
        default_factory=dict,
        description="Numeric tuning for detectors that expose thresholds "
                    "(e.g. the cardinality detectors' 'ratio' / 'floor'). "
                    "Keys are detector-defined; unknown keys are ignored. "
                    "Empty = the detector's built-in defaults.",
    )


class StewardSettings(BaseModel):
    """User-tunable behaviour. All fields are validated server-side
    so a malformed PUT can't put the scan path in a bad state."""

    enabled: bool = Field(
        default=True,
        description=(
            "Master kill-switch. When false, /api/steward/findings "
            "returns an empty list and the header badge hides itself. "
            "Useful for very large workspaces evaluating the feature."
        ),
    )
    min_severity: Literal["p1", "p2", "p3"] = Field(
        default="p3",
        description=(
            "Hide findings below this severity. P3 shows everything; "
            "P2 hides informational findings; P1 shows only "
            "production-blocker-level."
        ),
    )
    scan_on_save: bool = Field(
        default=True,
        description=(
            "If true, the frontend dispatches `fpulse:steward-refresh` "
            "after every workflow save so duplicate-source findings "
            "appear immediately. If false, only the periodic 60s poll "
            "+ the manual Re-scan button trigger scans."
        ),
    )
    auto_stale_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description=(
            "Findings open for this many days without dismiss/resolve "
            "auto-age into 'stale' status. Hidden from the default view "
            "but kept in memory for the curator."
        ),
    )
    escalate_after_n_occurrences: int = Field(
        default=5,
        ge=2,
        le=50,
        description=(
            "When a finding has been emitted in this many separate scans "
            "without resolution, its severity bumps one step (P3 -> P2 -> P1). "
            "Set high to suppress escalation, low to be aggressive."
        ),
    )
    escalate_min_hours_since_first: int = Field(
        default=24,
        ge=0,
        le=720,
        description=(
            "Minimum age (hours) of the FIRST emit before severity "
            "escalation kicks in. Time-clamps the occurrence counter so "
            "a 60-second cron job hitting 5 scans in 5 minutes does NOT "
            "page-out to P1. Default 24h (one operator workday). Set to "
            "0 to disable the time clamp entirely and rely only on the "
            "raw count."
        ),
    )
    notify_on_finding: bool = Field(
        default=True,
        description=(
            "When true, NEW or NEWLY-ESCALATED findings also write a row "
            "to the in-app notification bell (and trigger any configured "
            "email / Slack channels). De-dup is enforced — re-scans of "
            "unchanged findings never spam the bell."
        ),
    )
    notify_min_severity: Literal["p1", "p2", "p3"] = Field(
        default="p2",
        description=(
            "Notifications only fire at this severity or higher. Default "
            "P2 means information-only P3 findings stay in the eye-icon "
            "badge but don't ping the bell. Set to P1 for least-noisy "
            "behaviour, P3 for everything."
        ),
    )
    detectors: dict[str, DetectorOverride] = Field(
        default_factory=dict,
        description=(
            "Per-detector overrides, keyed by finding-kind (e.g. "
            "'join_explosion'). Each entry can disable the detector or "
            "override its severity. Detectors not listed here run with "
            "their built-in defaults. Surfaced + edited on the Coverage page."
        ),
    )


class SettingsStore:
    """Tiny JSON-file persistence layer. One file per workspace."""

    def __init__(self, settings_path: Path):
        self.path = settings_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> StewardSettings:
        if not self.path.is_file():
            return StewardSettings()
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
            return StewardSettings.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            # Corrupt or unparseable — fall back to defaults so the
            # scan path keeps working. The /settings GET will return
            # the defaults and the user can re-PUT to overwrite.
            return StewardSettings()

    def save(self, settings: StewardSettings) -> None:
        with _FILE_LOCK:
            with self.path.open("w", encoding="utf-8") as fp:
                json.dump(settings.model_dump(), fp, indent=2)
