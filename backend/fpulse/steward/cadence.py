"""F-Pulse Steward — cadence: automatic run-cadence (freshness) anomaly.

Activates ``FindingKind.CADENCE_MISS`` (DATA level): "this source normally runs
every ~24h; it hasn't run in 3 days." The automatic, threshold-free companion to
the Quality engine's user-set ``FRESHNESS_MISS`` — the same relationship foreseer
(``VOLUME_ANOMALY``) has to quality's ``row_count_min`` / ``row_count_max``.

# Hard Rule 6 — Historical Baseline Variance, not absolute thresholds

The cadence is *learned* from the gaps between this source's own prior runs
(median / MAD), never a fixed max-age. A source that runs erratically (high
jitter relative to its median interval) is never held to an SLA it never had, so
an ad-hoc / on-demand source is not flagged for "being late".

# Statistics — same robust core as foreseer (Hard Rule 3)

Median + MAD + the modified z-score ``0.6745 * (gap - median) / MAD``. Robust
estimators keep a single long historical gap (a past outage) from inflating the
band and muting detection afterward.

# Data source — no new ingestion

Reads the SAME ``CostEvent`` log foreseer / cost already record: the
``completed_at`` timestamps per ``source_signature``. Pure function over that
history plus ``now``; deterministic finding ids so each scan is idempotent.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)

if TYPE_CHECKING:  # CostEvent is a plain model; avoid a runtime import cycle
    from .cost import CostEvent


# ── Tunables ─────────────────────────────────────────────────────────
_MIN_HISTORY = 5          # prior INTERVALS required (=> min_history + 1 runs)
_Z_THRESHOLD = 3.5        # modified z-score outlier cutoff (Iglewicz & Hoaglin)
_MIN_RATIO_DELTA = 0.5    # the overdue gap must exceed the cadence by >= 50%
_MAX_JITTER_RATIO = 0.5   # MAD/median above this => no real cadence, skip


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(*parts: str) -> str:
    raw = "::".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── Robust statistics (pure, dependency-free) ────────────────────────


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mad(xs: list[float], med: float) -> float:
    if not xs:
        return 0.0
    return _median([abs(x - med) for x in xs])


def modified_zscore(value: float, history: list[float]) -> float:
    med = _median(history)
    mad = _mad(history, med)
    if mad == 0:
        return 0.0
    return 0.6745 * (value - med) / mad


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s >= 86400:
        return f"{s / 86400:.1f}d"
    if s >= 3600:
        return f"{s / 3600:.1f}h"
    if s >= 60:
        return f"{s / 60:.0f}m"
    return f"{s}s"


# ── Detector ─────────────────────────────────────────────────────────


def detect_cadence_misses(
    events: list["CostEvent"],
    *,
    now: datetime | None = None,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
    min_history: int = _MIN_HISTORY,
    z_threshold: float = _Z_THRESHOLD,
) -> list[StewardFinding]:
    """Compute CADENCE_MISS findings from a CostEvent history.

    For each source with a learned, regular run-cadence, flags the source when
    the time since its last run (``now`` - last ``completed_at``) is anomalously
    large versus the robust baseline of its prior inter-run intervals.
    """
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    suppressed = suppressed_signatures or set()

    by_sig: dict[str, list["CostEvent"]] = defaultdict(list)
    for e in events:
        if getattr(e, "source_signature", ""):
            by_sig[e.source_signature].append(e)

    out: list[StewardFinding] = []
    for sig, evs in by_sig.items():
        times = sorted(
            t for t in (
                _parse(getattr(e, "completed_at", None) or getattr(e, "recorded_at", None))
                for e in evs
            ) if t is not None
        )
        if len(times) < min_history + 1:
            continue

        intervals = [(times[i] - times[i - 1]).total_seconds() for i in range(1, len(times))]
        intervals = [x for x in intervals if x > 0]  # drop same-timestamp dupes
        if len(intervals) < min_history:
            continue

        med = _median(intervals)
        mad = _mad(intervals, med)
        if med <= 0:
            continue
        # No real cadence (too jittery to have an implicit SLA) -> don't flag.
        if mad > 0 and (mad / med) > _MAX_JITTER_RATIO:
            continue

        last = times[-1]
        gap = (now_dt - last).total_seconds()
        if gap <= med:
            continue  # not overdue
        if (gap - med) / med < _MIN_RATIO_DELTA:
            continue  # overdue, but not by enough to matter

        if mad > 0:
            z = modified_zscore(gap, intervals)
            if z < z_threshold:
                continue
        else:
            # Perfectly regular cadence, now overdue past the ratio guard: real.
            z = z_threshold

        sig_hash = _signature("cadence_miss", workspace_id, sig)
        if sig_hash in suppressed:
            continue

        last_ev = max(
            evs,
            key=lambda e: _parse(getattr(e, "completed_at", None) or getattr(e, "recorded_at", None)) or now_dt,
        )
        out.append(_build_cadence_finding(
            underlying_sig=sig, sig_hash=sig_hash, last_ev=last_ev,
            intervals=intervals, med=med, mad=mad, gap=gap, zscore=z,
            last=last, workspace_id=workspace_id,
        ))
    return out


def _build_cadence_finding(
    *, underlying_sig, sig_hash, last_ev, intervals, med, mad, gap, zscore, last, workspace_id,
) -> StewardFinding:
    fid = f"cad-{sig_hash[:12]}"
    now = _iso_now()
    sample = len(intervals)
    overdue_ratio = gap / med if med > 0 else 0.0

    title = f"Source overdue: last ran {_fmt_dur(gap)} ago vs its ~{_fmt_dur(med)} cadence"
    body = (
        f"A source that normally runs about every **{_fmt_dur(med)}** "
        f"(learned from {sample} prior intervals) last completed **{_fmt_dur(gap)} ago** "
        f"— {overdue_ratio:.1f}x its usual cadence (robust z-score {zscore:+.1f}).\n\n"
        f"This is flagged against the source's **own learned run-cadence**, not a "
        f"fixed max-age, so it only fires when the pipeline genuinely stopped running "
        f"on schedule. Likely causes:\n"
        f"- The schedule was disabled or the trigger stopped firing\n"
        f"- An upstream dependency the run waits on is stuck\n"
        f"- The pipeline errors early every run and never records a completion\n\n"
        f"Dismiss if this is expected (a paused pipeline, a seasonal / on-demand source)."
    )

    confidence = "high" if sample >= 10 else "medium"
    z_factor = min(1.0, abs(zscore) / (2.0 * _Z_THRESHOLD))
    size_factor = min(1.0, sample / 12.0)
    confidence_score = round(max(0.5, min(0.99, 0.5 + 0.49 * z_factor * size_factor)), 2)

    return StewardFinding(
        id=fid,
        workspace_id=workspace_id,
        kind=FindingKind.CADENCE_MISS,
        level=FindingLevel.DATA,
        severity=FindingSeverity.P2,
        status=FindingStatus.OPEN,
        title=title,
        body=body,
        evidence={
            "source_signature": sig_hash,
            "underlying_source_signature": underlying_sig,
            "pipeline_id": getattr(last_ev, "pipeline_id", None),
            "pipeline_name": getattr(last_ev, "pipeline_name", None),
            "run_id": getattr(last_ev, "run_id", None),
            "last_completed_at": last.isoformat(),
            "gap_seconds": round(gap, 1),
            "baseline_median_seconds": round(med, 1),
            "baseline_mad_seconds": round(mad, 1),
            "overdue_ratio": round(overdue_ratio, 2),
            "modified_zscore": round(zscore, 2),
            "sample_size": sample,
        },
        proposed_actions=[
            {
                "label": "Dismiss (expected — paused / seasonal / on-demand)",
                "action": "suppress_finding",
                "params": {"finding_id": fid, "scope": "signature"},
            },
        ],
        first_seen=now,
        last_seen=now,
        occurrences=1,
        confidence=confidence,
        confidence_score=confidence_score,
        evidence_count=sample,
        baseline_window=f"last_{sample}_intervals",
    )
