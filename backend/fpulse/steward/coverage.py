"""Steward coverage registry (2026-06-18).

The "what is the Steward actually watching?" surface — the honest answer to
the user's "closed-loop proof" gap. Two rules make this trustworthy:

  1. **Only ships detectors that genuinely run.** Every entry below maps to a
     detector wired into ``api/steward.py:_run_scan`` or one of the
     event/run recorders (schema-drift, quality, pii, row-delta, …).
     Contract-only kinds (JOIN_EXPLOSION, SLA_BREACH, COST_DRIFT, …) that
     have no shipping detector are deliberately EXCLUDED — listing them
     would be the exact decorative/fake-coverage claim we refuse to make.
  2. **Counts come from the live scan**, not this file. The endpoint merges
     each detector's real open-finding count from the current scan.

``mode`` tells the user HOW each detector observes:
  * ``state`` — re-derived from current workspace state on every scan
                (duplicates, connector health, governance, warehouse waste).
  * ``event`` — recorded the moment something happens (schema drift, a
                quality assertion, a PII-suggestive column) and re-surfaced.
  * ``run``   — fed by completed pipeline-run metrics (volume anomaly,
                empty output, row-count integrity).
"""
from __future__ import annotations

from typing import Any

from .models import FindingKind, level_for_kind


# Each entry: kind (authoritative enum value), human label, observe mode,
# one-line description. Level is derived from KIND_TO_LEVEL so it can't drift.
_ACTIVE_DETECTORS: list[dict[str, str]] = [
    # ── Architecture / structural ──
    {"kind": FindingKind.DUPLICATE_SOURCE.value, "label": "Duplicate source", "mode": "state",
     "description": "Multiple pipelines reading the same table, file, or API."},
    {"kind": FindingKind.DUPLICATE_PIPELINE.value, "label": "Duplicate pipeline", "mode": "state",
     "description": "Near-identical pipelines that likely duplicate work."},
    {"kind": FindingKind.ORPHANED_TABLE.value, "label": "Unused managed table", "mode": "state",
     "description": "A managed table no pipeline reads or writes (likely leftover)."},
    # ── Connector reliability ──
    {"kind": FindingKind.CONNECTOR_AUTH_FAILURE.value, "label": "Connector auth failure", "mode": "state",
     "description": "A connection failed authentication on its last test."},
    {"kind": FindingKind.CONNECTOR_RATE_LIMIT.value, "label": "Connector rate limit", "mode": "state",
     "description": "A connector is being throttled by its upstream provider."},
    {"kind": FindingKind.CONNECTOR_UNREACHABLE.value, "label": "Connector unreachable", "mode": "state",
     "description": "A connection could not be reached on its last test."},
    {"kind": FindingKind.CREDENTIAL_NEAR_EXPIRY.value, "label": "Credential expiring", "mode": "state",
     "description": "A stored credential is close to its expiry date."},
    # ── Data ──
    {"kind": FindingKind.SCHEMA_DRIFT.value, "label": "Schema drift", "mode": "event",
     "description": "A source's columns or types changed between runs."},
    {"kind": FindingKind.NULL_SPIKE.value, "label": "Null spike", "mode": "event",
     "description": "A column's null rate jumped above its baseline."},
    {"kind": FindingKind.DUPLICATE_KEY_SPIKE.value, "label": "Duplicate keys", "mode": "event",
     "description": "Unexpected duplicate values in a key column."},
    {"kind": FindingKind.VOLUME_ANOMALY.value, "label": "Volume anomaly", "mode": "run",
     "description": "A source's row volume deviated sharply from its baseline."},
    {"kind": FindingKind.FRESHNESS_MISS.value, "label": "Freshness miss", "mode": "event",
     "description": "Data is older than its expected freshness window."},
    {"kind": FindingKind.PARTITION_MISSING.value, "label": "Partition missing", "mode": "event",
     "description": "An expected partition did not arrive."},
    {"kind": FindingKind.QUALITY_CHECK_FAILED.value, "label": "Quality check failed", "mode": "event",
     "description": "A declared data-quality assertion failed."},
    # ── Node ──
    {"kind": FindingKind.EMPTY_OUTPUT.value, "label": "Empty output", "mode": "run",
     "description": "A node produced zero rows on a run that should have data."},
    {"kind": FindingKind.ROW_COUNT_DELTA.value, "label": "Row-count integrity", "mode": "run",
     "description": "A 1:1 step silently dropped or duplicated rows."},
    {"kind": FindingKind.JOIN_EXPLOSION.value, "label": "Join explosion", "mode": "run",
     "description": "A join produced far more rows than its inputs (near-cartesian)."},
    {"kind": FindingKind.JOIN_COLLAPSE.value, "label": "Join collapse", "mode": "run",
     "description": "A join matched almost nothing (likely a key mismatch)."},
    {"kind": FindingKind.DEDUPE_COLLAPSE.value, "label": "Dedupe over-removal", "mode": "run",
     "description": "Deduplicate removed almost every row (key too coarse)."},
    {"kind": FindingKind.FILTER_DROPPED_ALL.value, "label": "Filter dropped all", "mode": "run",
     "description": "A filter removed every input row (predicate likely wrong)."},
    # ── Governance ──
    {"kind": FindingKind.PII_LEAK.value, "label": "PII exposure", "mode": "event",
     "description": "PII-suggestive columns detected in an output schema."},
    {"kind": FindingKind.ENV_CROSSING.value, "label": "Environment crossing", "mode": "state",
     "description": "A dev resource used in a prod context, or vice-versa."},
    {"kind": FindingKind.UNAPPROVED_DESTINATION.value, "label": "Unapproved destination", "mode": "state",
     "description": "A sink writes outside the configured governance policy."},
    # ── Cost ──
    {"kind": FindingKind.WAREHOUSE_WASTE.value, "label": "Warehouse waste", "mode": "state",
     "description": "Spend or compute-waste signals on warehouse usage."},
    # ── Custom ──
    {"kind": FindingKind.USER_DEFINED.value, "label": "Custom rules", "mode": "state",
     "description": "Findings from your own Steward rule files."},
]


def coverage_detectors() -> list[dict[str, Any]]:
    """The active-detector registry, each enriched with its observability
    level (from the single-source-of-truth KIND_TO_LEVEL map)."""
    out: list[dict[str, Any]] = []
    for d in _ACTIVE_DETECTORS:
        try:
            level = level_for_kind(FindingKind(d["kind"])).value
        except Exception:  # noqa: BLE001 — unknown kind never breaks the page
            level = "pipeline"
        out.append({**d, "level": level})
    return out


__all__ = ["coverage_detectors"]
