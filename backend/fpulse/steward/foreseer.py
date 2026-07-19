"""F-Pulse Steward — foreseer: volumetric anomaly detection (2026-06-08).

First shipping cut of the `foreseer` sub-agent (named in the v1.3
roadmap, see ``steward/__init__.py``). It activates
``FindingKind.VOLUME_ANOMALY`` (DATA level): "this source normally
delivers ~10k rows; today it delivered 0 / 80k."

# Why this exists (reviewer rec, 2026-06-08)

An external review flagged "sudden row-count drops/spikes" as a missing
*automatic* detector. F-Pulse already had **threshold** volume checks
(``quality.py`` ``row_count_min`` / ``row_count_max``) but those require
the user to pick the numbers up-front. This module needs no thresholds:
it learns each source's normal volume from history and flags deviations.

# Hard Rule 6 — Historical Baseline Variance, not absolute thresholds

Per the Steward architectural invariants (``__init__.py``), volume
alerts MUST compare against an observed per-signature baseline, never a
fixed number. So:

  * A source that returns 0 rows most of the time is NOT flagged on the
    next zero day (the "valid empty table" fallacy).
  * A source that returns 10k +/- 500 rows daily IS flagged when it
    returns 0 — or when it spikes to 80k.

# Statistics (deterministic core — Hard Rule 3)

We use **robust** estimators — the median and the MAD (median absolute
deviation) — and the modified z-score::

    z = 0.6745 * (value - median) / MAD

Robust estimators (not mean / standard deviation) are deliberate: a
single past backfill spike would inflate a stddev band and then *mute*
detection for weeks. The median/MAD band ignores that outlier. 0.6745
scales the MAD to be a consistent estimator of the standard deviation
for normally-distributed data, so the usual ~3.5 outlier cutoff applies.

Three guards keep it quiet on noisy little tables:

  1. ``min_history`` prior runs required before judging (default 5).
  2. a **material** absolute change (default >= 50 rows) — ignore
     7 -> 12 row jitter even if it is statistically "large".
  3. a **relative** change (default >= 50%) — the swing must matter.

# Data source — no new ingestion

foreseer reads the SAME ``CostEvent`` log the cost detector already
records (``rows_read`` per ``source_signature`` per run). It is a pure
function over that history, recomputed each scan — no extra storage, no
extra POST endpoint. ``rows_read`` (source input volume) is used rather
than ``rows_written`` so this never double-fires with the cost
detector's WAREHOUSE_WASTE / EMPTY_OUTPUT signals (which key on
``rows_written``).
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

if TYPE_CHECKING:  # avoid a runtime import cycle; CostEvent is a plain model
    from .cost import CostEvent


# ── Tunables ─────────────────────────────────────────────────────────
# Minimum number of PRIOR runs (excluding the run under judgement)
# required before we'll call anything anomalous. Five gives the
# median/MAD something real to stand on.
_MIN_HISTORY = 5
# Modified z-score cutoff. 3.5 is the canonical outlier threshold for
# the MAD-based modified z-score (Iglewicz & Hoaglin).
_Z_THRESHOLD = 3.5
# Ignore swings smaller than this many rows in absolute terms — kills
# noise on tiny tables where a few rows is a huge *relative* move.
_MIN_MATERIAL_ROWS = 50
# Ignore swings smaller than this fraction of the baseline — the change
# has to actually matter, not just be statistically detectable.
_MIN_RATIO_DELTA = 0.5


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
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _mad(xs: list[float], med: float) -> float:
    """Median Absolute Deviation about ``med``."""
    if not xs:
        return 0.0
    return _median([abs(x - med) for x in xs])


def modified_zscore(value: float, history: list[float]) -> float:
    """Robust modified z-score of ``value`` against ``history``.

    Returns 0.0 when the history has no spread (MAD == 0) — the caller
    handles that perfectly-stable case explicitly so a divide-by-zero
    never silently swallows a real change.
    """
    med = _median(history)
    mad = _mad(history, med)
    if mad == 0:
        return 0.0
    return 0.6745 * (value - med) / mad


# ── Detector ─────────────────────────────────────────────────────────


def detect_volume_anomalies(
    events: list["CostEvent"],
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
    min_history: int = _MIN_HISTORY,
    z_threshold: float = _Z_THRESHOLD,
) -> list[StewardFinding]:
    """Compute VOLUME_ANOMALY findings from a CostEvent history.

    Groups events by ``source_signature`` and, for each source with
    enough history, judges the most recent run's ``rows_read`` against
    the robust baseline of all PRIOR runs. Pure + idempotent: the same
    history yields the same deterministic finding ids, so re-running each
    scan is safe.
    """
    suppressed = suppressed_signatures or set()

    by_sig: dict[str, list["CostEvent"]] = defaultdict(list)
    for e in events:
        if getattr(e, "source_signature", ""):
            by_sig[e.source_signature].append(e)

    out: list[StewardFinding] = []
    for sig, evs in by_sig.items():
        if len(evs) < min_history + 1:
            continue
        evs = sorted(evs, key=lambda e: e.completed_at or e.recorded_at)
        current = evs[-1]
        history = evs[:-1]
        series = [float(e.rows_read) for e in history]
        cur_val = float(current.rows_read)

        med = _median(series)
        mad = _mad(series, med)

        # Rule 6 valid-empty guard: a usually-empty source is not flagged
        # for being empty again.
        if med == 0 and cur_val == 0:
            continue
        # Material absolute-change guard (noise on tiny tables).
        if abs(cur_val - med) < _MIN_MATERIAL_ROWS:
            continue
        # Relative-change guard (the move has to matter).
        denom = med if med > 0 else 1.0
        if abs(cur_val - med) / denom < _MIN_RATIO_DELTA:
            continue

        if mad > 0:
            z = modified_zscore(cur_val, series)
            if abs(z) < z_threshold:
                continue
        else:
            # Perfectly stable baseline (every prior run identical). The
            # material + relative guards already passed, so this is a real
            # break from a dead-steady signal. Synthesise a representative
            # z at the threshold so downstream copy/confidence read right.
            z = z_threshold if cur_val > med else -z_threshold

        sig_hash = _signature("volume_anomaly", workspace_id, sig)
        if sig_hash in suppressed:
            continue

        out.append(
            _build_volume_anomaly(
                underlying_sig=sig,
                sig_hash=sig_hash,
                current=current,
                series=series,
                median=med,
                mad=mad,
                zscore=z,
                cur_val=cur_val,
                workspace_id=workspace_id,
            )
        )
    return out


def _build_volume_anomaly(
    *,
    underlying_sig: str,
    sig_hash: str,
    current: "CostEvent",
    series: list[float],
    median: float,
    mad: float,
    zscore: float,
    cur_val: float,
    workspace_id: str,
) -> StewardFinding:
    is_drop = cur_val < median
    direction = "drop" if is_drop else "spike"
    denom = median if median > 0 else 1.0
    pct = (cur_val - median) / denom * 100.0

    fid = f"vol-{sig_hash[:12]}"
    now = _iso_now()
    sample_size = len(series)
    # Show the tail of the baseline plus the anomalous value for context.
    recent = [int(v) for v in series[-7:]] + [int(cur_val)]

    if is_drop:
        verb = "dropped"
        causes = (
            "- An upstream source outage or a filter that now drops most rows\n"
            "- An incremental cursor stuck on a stale watermark (no new rows seen)\n"
            "- A partition / date predicate that no longer matches where data lives"
        )
    else:
        verb = "spiked"
        causes = (
            "- A backfill or replay that re-read historical data\n"
            "- A duplicate load (the same source ingested twice)\n"
            "- An incremental cursor reset, pulling the full table again"
        )

    title = (
        f"Source volume {verb} {abs(pct):.0f}% vs its {sample_size}-run baseline"
    )
    body = (
        f"A source that normally reads about **{median:.0f} rows** per run "
        f"just read **{cur_val:.0f} rows** — a {abs(pct):.0f}% {direction} "
        f"(robust z-score {zscore:+.1f}, baseline MAD {mad:.0f} over "
        f"{sample_size} prior runs).\n\n"
        f"This is flagged against the source's **own learned baseline**, "
        f"not a fixed threshold, so it only fires when the volume genuinely "
        f"breaks from history. Likely causes:\n"
        f"{causes}\n\n"
        f"Recent rows_read (oldest -> newest, last value is this run): "
        f"{recent}\n\n"
        f"Dismiss if this change is expected (a planned backfill, a "
        f"seasonal source, a one-off reload)."
    )

    confidence = "high" if sample_size >= 10 else "medium"
    # Scale 0.5 .. 0.99 by how far past the threshold we are, tempered by
    # sample size so a 6-run baseline never reads as fully confident.
    z_factor = min(1.0, abs(zscore) / (2.0 * _Z_THRESHOLD))
    size_factor = min(1.0, sample_size / 12.0)
    confidence_score = round(max(0.5, min(0.99, 0.5 + 0.49 * z_factor * size_factor)), 2)

    return StewardFinding(
        id=fid,
        workspace_id=workspace_id,
        kind=FindingKind.VOLUME_ANOMALY,
        level=FindingLevel.DATA,
        severity=FindingSeverity.P2,
        status=FindingStatus.OPEN,
        title=title,
        body=body,
        evidence={
            # Hash is the suppression key (matches every other detector).
            "source_signature": sig_hash,
            "underlying_source_signature": underlying_sig,
            "pipeline_id": current.pipeline_id,
            "pipeline_name": current.pipeline_name,
            "run_id": current.run_id,
            "direction": direction,
            "current_rows_read": int(cur_val),
            "baseline_median": round(median, 2),
            "baseline_mad": round(mad, 2),
            "modified_zscore": round(zscore, 2),
            "pct_change": round(pct, 1),
            "sample_size": sample_size,
            "recent_rows_read": recent,
        },
        proposed_actions=[
            {
                "label": "Dismiss (expected — backfill / seasonal / reload)",
                "action": "suppress_finding",
                "params": {"finding_id": fid, "scope": "signature"},
            },
        ],
        first_seen=current.completed_at or current.recorded_at or now,
        last_seen=now,
        occurrences=1,
        confidence=confidence,
        confidence_score=confidence_score,
        evidence_count=sample_size,
        baseline_window=f"last_{sample_size}_runs",
    )
