"""F-Pulse Steward — null-rate: automatic per-column null-rate anomaly.

Activates ``FindingKind.NULL_RATE_ANOMALY`` (DATA level): "column ``email``
normally has ~1% nulls; this run it's 12%." The automatic, threshold-free
companion to the Quality engine's assertion-driven ``NULL_SPIKE`` — it flags a
null-rate that breaks from the column's OWN learned baseline, catching a slow
creep or a sudden spike *before* it trips a hard `not_null` assertion (or on
columns whose assertion is lenient / advisory).

# Data source — no new counting cost (Hard Rule: nulls aren't free)

Counting nulls is expensive, so F-Pulse never scans a whole table just to learn.
Instead this rides the counts a runner ALREADY computed for its ``not_null``
assertions: ``samples_from_report`` turns each report's ``not_null`` assertion
(``failed_count`` / ``total_rows``) into one ``NullRateSample``, appended to a
per-workspace JSONL. The detector is a pure baseline-variance pass over that
series — so coverage is exactly the columns the operator already cares about
nulls in, at zero extra query cost.

# Hard Rule 6 — Historical Baseline Variance, not absolute thresholds

Robust median/MAD + modified z-score over the column's prior null rates. Only
INCREASES are flagged (a null spike, never an improvement), and only when the
move is material in both absolute (>= 2 percentage points) and relative terms.
"""

from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .foreseer import modified_zscore
from .models import (
    FindingKind,
    FindingLevel,
    FindingSeverity,
    FindingStatus,
    StewardFinding,
)

if TYPE_CHECKING:  # avoid a runtime import cycle
    from .quality import QualityCheckReport

_MIN_HISTORY = 5          # prior samples required before judging
_Z_THRESHOLD = 3.5
_MIN_RATE_DELTA = 0.02    # ignore null-rate moves smaller than 2 percentage points
_MIN_RATIO_DELTA = 0.5    # the increase must be >= 50% of the baseline
_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(*parts: str) -> str:
    raw = "::".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mad(xs: list[float], med: float) -> float:
    return _median([abs(x - med) for x in xs]) if xs else 0.0


# ── Sample model + store ─────────────────────────────────────────────


class NullRateSample(BaseModel):
    """One column's null rate on one run (from a not_null assertion)."""

    source_signature: str
    column: str
    null_rate: float          # 0.0 .. 1.0
    total_rows: int = 0
    run_id: str = ""
    at: str = ""


def samples_from_report(report: "QualityCheckReport") -> list[NullRateSample]:
    """Extract per-column null-rate samples from a report's not_null checks."""
    now = _iso_now()
    out: list[NullRateSample] = []
    for a in report.assertions:
        if a.check != "not_null" or not a.column or a.total_rows <= 0:
            continue
        out.append(NullRateSample(
            source_signature=report.source_signature,
            column=a.column,
            null_rate=a.failed_count / a.total_rows,
            total_rows=a.total_rows,
            run_id=report.run_id,
            at=now,
        ))
    return out


class NullRateSampleStore:
    """Append-only JSONL of per-column null-rate samples (grep/jq friendly)."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_many(self, samples: list[NullRateSample]) -> None:
        if not samples:
            return
        with _FILE_LOCK, open(self.path, "a", encoding="utf-8") as f:
            for s in samples:
                f.write(s.model_dump_json() + "\n")

    def all(self) -> list[NullRateSample]:
        if not self.path.exists():
            return []
        out: list[NullRateSample] = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(NullRateSample.model_validate_json(line))
                except Exception:  # noqa: BLE001 — skip a corrupt line, keep the rest
                    pass
        return out


# ── Detector ─────────────────────────────────────────────────────────


def detect_null_rate_anomalies(
    samples: list[NullRateSample],
    *,
    workspace_id: str = "default",
    suppressed_signatures: set[str] | None = None,
    min_history: int = _MIN_HISTORY,
    z_threshold: float = _Z_THRESHOLD,
) -> list[StewardFinding]:
    """Flag per (source, column) null-rate INCREASES vs the learned baseline."""
    suppressed = suppressed_signatures or set()

    by_key: dict[tuple[str, str], list[NullRateSample]] = defaultdict(list)
    for s in samples:
        by_key[(s.source_signature, s.column)].append(s)

    out: list[StewardFinding] = []
    for (src, col), ss in by_key.items():
        if len(ss) < min_history + 1:
            continue
        ss = sorted(ss, key=lambda s: s.at)
        current = ss[-1]
        series = [float(s.null_rate) for s in ss[:-1]]
        cur_val = float(current.null_rate)

        med = _median(series)
        mad = _mad(series, med)

        if cur_val <= med:
            continue  # only spikes, never improvements
        if (cur_val - med) < _MIN_RATE_DELTA:
            continue  # material in absolute (percentage-point) terms
        denom = med if med > 0 else _MIN_RATE_DELTA
        if (cur_val - med) / denom < _MIN_RATIO_DELTA:
            continue  # material in relative terms

        if mad > 0:
            z = modified_zscore(cur_val, series)
            if z < z_threshold:
                continue
        else:
            z = z_threshold  # dead-steady baseline, now clearly up: real

        sig_hash = _signature("null_rate_anomaly", workspace_id, src, col)
        if sig_hash in suppressed:
            continue

        out.append(_build_null_rate_finding(
            src=src, col=col, sig_hash=sig_hash, current=current,
            series=series, med=med, mad=mad, zscore=z, cur_val=cur_val,
            workspace_id=workspace_id,
        ))
    return out


def _build_null_rate_finding(
    *, src, col, sig_hash, current, series, med, mad, zscore, cur_val, workspace_id,
) -> StewardFinding:
    fid = f"nra-{sig_hash[:12]}"
    now = _iso_now()
    sample = len(series)
    cur_pct = cur_val * 100.0
    med_pct = med * 100.0
    recent = [round(v * 100.0, 1) for v in series[-7:]] + [round(cur_pct, 1)]

    title = f"Null rate on `{col}` jumped to {cur_pct:.1f}% vs its ~{med_pct:.1f}% baseline"
    body = (
        f"Column **`{col}`** normally runs about **{med_pct:.1f}% nulls** "
        f"(over {sample} prior runs) but this run it was **{cur_pct:.1f}%** "
        f"(robust z-score {zscore:+.1f}).\n\n"
        f"This is flagged against the column's **own learned null-rate**, not a "
        f"fixed threshold, so it fires on a genuine break from history — earlier "
        f"than a hard `not_null` assertion would. Likely causes:\n"
        f"- An upstream schema/source change dropped or renamed the field\n"
        f"- A join now misses rows that used to populate it\n"
        f"- A parsing/mapping change is emitting nulls where it didn't\n\n"
        f"Recent null-rate % (oldest -> newest, last is this run): {recent}\n\n"
        f"Dismiss if expected (a new optional column, a known backfill gap)."
    )

    confidence = "high" if sample >= 10 else "medium"
    z_factor = min(1.0, abs(zscore) / (2.0 * _Z_THRESHOLD))
    size_factor = min(1.0, sample / 12.0)
    confidence_score = round(max(0.5, min(0.99, 0.5 + 0.49 * z_factor * size_factor)), 2)

    return StewardFinding(
        id=fid,
        workspace_id=workspace_id,
        kind=FindingKind.NULL_RATE_ANOMALY,
        level=FindingLevel.DATA,
        severity=FindingSeverity.P2,
        status=FindingStatus.OPEN,
        title=title,
        body=body,
        evidence={
            "source_signature": sig_hash,
            "underlying_source_signature": src,
            "column": col,
            "run_id": getattr(current, "run_id", ""),
            "current_null_rate": round(cur_val, 4),
            "baseline_median": round(med, 4),
            "baseline_mad": round(mad, 4),
            "modified_zscore": round(zscore, 2),
            "sample_size": sample,
            "recent_null_pct": recent,
        },
        proposed_actions=[
            {
                "label": "Dismiss (expected — new optional column / known gap)",
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
        baseline_window=f"last_{sample}_runs",
    )
