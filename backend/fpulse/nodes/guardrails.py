"""Source-node guardrails — file size and row count limits.

These checks run BEFORE DuckDB opens a file (size guard) and AFTER a
relation is produced (row guard).  The limits come from
``fpulse.runtime_config`` so they are mode-aware (dev is permissive,
prod enforces caps).

Why here and not in middleware?  Middleware sees HTTP request size, not
data-file size.  A node that reads from the local filesystem or a remote
object store bypasses request size entirely.  These guardrails sit at the
data boundary, right where the damage happens.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

# Stage 2.5b: duckdb only used for type annotations on cap_rows.
if TYPE_CHECKING:
    import duckdb

from fpulse import runtime_config

logger = logging.getLogger(__name__)


class SourceGuardrailError(Exception):
    """Raised when a source exceeds a configured limit."""


def check_file_size(file_path: str) -> None:
    """Raise if the file exceeds ``MAX_UPLOAD_MB``.

    Only enforced when the limit is > 0.  A limit of 0 means "no cap"
    (useful for dev boxes processing large sample data).
    """
    cap = runtime_config.MAX_UPLOAD_MB
    if cap <= 0:
        return
    try:
        size_mb = os.path.getsize(file_path) / (1024 * 1024)
    except OSError:
        return  # file missing → let DuckDB report the real error
    if size_mb > cap:
        raise SourceGuardrailError(
            f"File {os.path.basename(file_path)} is {size_mb:.1f} MB which "
            f"exceeds the {cap} MB limit. Reduce the file or raise "
            f"FPULSE_MAX_UPLOAD_MB (current: {cap})."
        )


def cap_rows(
    relation: duckdb.DuckDBPyRelation,
    *,
    label: str = "source",
    full_run: bool = False,
) -> duckdb.DuckDBPyRelation:
    """Apply a LIMIT to the relation based on mode and run type.

    Two caps are checked in order:

    1. **Sample mode** (dev only, ``full_run=False``):
       Limits to ``DEV_SAMPLE_ROWS`` (default 1 M) for fast iteration.
       Skipped when ``full_run=True`` (explicit "Run Full" click) or
       when ``DEV_SAMPLE_ROWS`` is 0.

    2. **Hard cap** (``MAX_SOURCE_ROWS``):
       Absolute ceiling regardless of mode.  Prevents accidental
       SELECT * on a billion-row table.  0 = disabled.

    The LIMIT is pushed into DuckDB's query plan so it short-circuits
    early rather than materialising rows we'll throw away.
    """
    # ── Sample mode (dev preview) ─────────────────────────────────
    sample = runtime_config.DEV_SAMPLE_ROWS
    if sample > 0 and runtime_config.IS_DEV and not full_run:
        try:
            logger.info(
                "guardrails: %s — dev sample mode, limiting to %s rows",
                label, f"{sample:,}",
            )
            relation = relation.limit(sample)
        except Exception:
            logger.warning(
                "guardrails: could not apply dev sample to %s", label,
            )

    # ── Hard row cap ──────────────────────────────────────────────
    cap = runtime_config.MAX_SOURCE_ROWS
    if cap > 0:
        try:
            relation = relation.limit(cap)
        except Exception:
            logger.warning(
                "guardrails: could not apply row cap to %s — continuing uncapped",
                label,
            )
    return relation


def file_size_info(file_path: str) -> dict:
    """Return file-size metadata + volume tier for UX display.

    Called by source-node config panels (and the pre-execution check API)
    to show the user what they're about to process — before execution
    starts.  Returns a dict safe for JSON serialisation::

        {
            "file_path": "/data/sales.csv",
            "size_bytes": 2_147_483_648,
            "size_label": "2.0 GB",
            "tier": "caution",
            "tier_label": "Large — spill-to-disk active",
            "tier_color": "amber",
            "warning": "...",            # only present for caution+
            "scale_up_hint": "..."       # only present for warn/exceeds
        }
    """
    try:
        size_bytes = os.path.getsize(file_path)
    except OSError:
        return {"file_path": file_path, "size_bytes": 0, "size_label": "unknown"}

    # Human-readable size
    if size_bytes < 1024 * 1024:
        size_label = f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        size_label = f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        size_label = f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"

    tier = runtime_config.volume_tier(size_bytes)
    result: dict = {
        "file_path": file_path,
        "size_bytes": size_bytes,
        "size_label": size_label,
        "tier": tier["tier"],
        "tier_label": tier["label"],
        "tier_color": tier["color"],
    }
    if "warning" in tier:
        result["warning"] = tier["warning"]

    # Scale-up hint for large workloads
    if tier["tier"] in ("warn", "exceeds"):
        result["scale_up_hint"] = (
            "This workload exceeds F-Pulse's single-node comfort zone. "
            "Consider a distributed execution engine for this volume."
        )
    return result
