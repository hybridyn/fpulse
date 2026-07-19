# Steward — Automatic Volume-Anomaly Detection (`foreseer`)

*Ships in F-Pulse OSS. Added 2026-06-08.*

The `foreseer` sub-agent watches how much data each source moves and
flags runs that break from that source's **own learned baseline** — a
sudden drop ("yesterday 10k rows, today 0") or spike ("normally 500,
today 80k"). It activates the `volume_anomaly` finding kind at the
**data** observability level.

## Threshold checks vs. automatic detection

F-Pulse already had **threshold** volume checks
(see [`quality-checks.md`](quality-checks.md): `row_count_min` /
`row_count_max`). Those are great when you *know* the numbers up front —
"this table should always have ≥ 1000 rows."

`foreseer` is different: it needs **no configured numbers**. It learns
the normal volume from history, so it catches anomalies on sources you
never wrote an assertion for. The two are complementary:

| | `row_count_min/max` (quality) | `foreseer` (volume_anomaly) |
|---|---|---|
| Setup | You pick the threshold | Zero config |
| Basis | Absolute number | The source's own history |
| Best for | Hard contracts | "Tell me when *anything* shifts" |

## How it decides (Hard Rule 6 — baseline variance, not thresholds)

For each source, `foreseer` builds a series of `rows_read` over its past
runs and judges the latest run against it:

- **Robust baseline.** It uses the **median** and the **MAD** (median
  absolute deviation), not the mean/standard deviation. A single past
  backfill spike would inflate a stddev band and then *mute* detection
  for weeks; the median/MAD band ignores that outlier.
- **Modified z-score.** `z = 0.6745 · (value − median) / MAD`. The
  0.6745 factor makes MAD a consistent estimator of the standard
  deviation for normal data, so the usual `|z| ≥ 3.5` outlier cutoff
  applies.
- **Guards against noise:**
  1. At least **5 prior runs** before it judges anything.
  2. A **material** absolute change (≥ 50 rows) — ignore 7 → 12 jitter.
  3. A **relative** change (≥ 50%) — the swing has to matter.
- **Valid-empty guard.** A source that is usually empty is **not**
  flagged for being empty again (the "valid empty table" fallacy that
  mutes monitoring). It only fires when a *non-trivial* baseline breaks.

This honours Steward architectural invariant #6: quantitative alerts
compare against an observed per-signature baseline, never an absolute
number.

## Where the data comes from (no extra setup)

`foreseer` reuses the **cost-event log** that already records per-run
volume — see [`cost-tracking.md`](cost-tracking.md). If you're posting
`rows_read` to `POST /api/steward/cost-event` (the F-Pulse executor does
this automatically), volume-anomaly detection works with **no
additional ingestion**. It is recomputed on each Steward scan from that
history, so there is no separate journal to manage.

It keys on `rows_read` (source input volume) specifically so it never
double-fires with the cost detector's `warehouse_waste` / `empty_output`
signals, which key on `rows_written`.

## What a finding looks like

- **Kind / level:** `volume_anomaly` / data
- **Severity:** P2 (conservative by default — advisor, not pager)
- **Evidence:** `direction` (drop/spike), `current_rows_read`,
  `baseline_median`, `baseline_mad`, `modified_zscore`, `pct_change`,
  `sample_size`, and a `recent_rows_read` tail for context.
- **Action:** Dismiss-with-reason if the change is expected (a planned
  backfill, a seasonal source, a one-off reload). Dismissal is
  remembered per signature, exactly like every other Steward finding.

The in-app finding renders a dedicated baseline→current chip
(e.g. `DROP  9 950 → 0 rows (−100%)`).

## Roadmap

This is the first cut of `foreseer`. The same baseline machinery is the
foundation for the rest of its planned data-level kinds (`null_spike`,
`duplicate_key_spike`, `freshness_miss`) and for `cost_drift` in the
Cost Steward — each compares against a learned baseline rather than a
fixed threshold.
