"""Run manifest — describes a completed (or in-progress) extraction.

A manifest is a JSON file written next to the output, capturing
everything an operator (or downstream loader) needs to know:
  - what profile / run_id produced this batch
  - row counts (extracted, failed, skipped_resumed)
  - schema fingerprint (so a downstream loader can detect drift)
  - timing (started_at, completed_at, duration_s)
  - output format + path + size
  - source freshness timestamp (when did the source last refresh)

The freshness gate reads the latest manifest for a profile to decide
whether enough time has elapsed since the last run.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any


def _schema_fingerprint(field_paths: dict[str, str], coercions: dict[str, str]) -> str:
    """Stable hash of (column → path, column → type). Changing any
    column name, path, or coercion changes the fingerprint — downstream
    loaders use this to detect drift between runs."""
    pairs: list[str] = []
    for col in sorted(field_paths):
        pairs.append(f"{col}={field_paths[col]}|{coercions.get(col, '')}")
    return hashlib.sha256("\n".join(pairs).encode("utf-8")).hexdigest()[:16]


@dataclass
class RunManifest:
    run_id: str
    profile_name: str
    started_at: float
    completed_at: float | None
    duration_s: float | None
    row_counts: dict[str, int] = field(default_factory=dict)
    schema_fingerprint: str = ""
    output_format: str = "jsonl"
    output_path: str = ""
    failed_path: str = ""
    output_size_bytes: int = 0
    source_freshness_at: float | None = None  # vendor "last collected" if known
    error: str | None = None

    # ── (de)serialise ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunManifest":
        # Tolerate unknown fields from forward-compatible writers.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    # ── Filesystem ───────────────────────────────────────────────────

    @staticmethod
    def filename(profile_name: str, run_id: str) -> str:
        # Profile-name first so glob-by-profile is cheap; run_id and a
        # completed_at timestamp let the latest() lookup pick newest.
        # We use ts in filename so listing the dir is enough — no need
        # to read every manifest to find the most recent.
        return f"{profile_name}__{run_id}.manifest.json"

    def save(self, manifest_dir: str) -> str:
        os.makedirs(manifest_dir, exist_ok=True)
        path = os.path.join(manifest_dir, self.filename(self.profile_name, self.run_id))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        return path

    @classmethod
    def latest(cls, manifest_dir: str, profile_name: str) -> "RunManifest | None":
        """Most-recently-completed manifest for `profile_name`, or None."""
        if not os.path.isdir(manifest_dir):
            return None
        candidates = glob.glob(
            os.path.join(manifest_dir, f"{profile_name}__*.manifest.json")
        )
        best: RunManifest | None = None
        best_completed: float = -1.0
        for path in candidates:
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            m = cls.from_dict(data)
            if m.completed_at is None:
                continue
            if m.completed_at > best_completed:
                best = m
                best_completed = m.completed_at
        return best


def schema_fingerprint_from_profile(profile) -> str:  # type: SourceProfile
    return _schema_fingerprint(profile.schema.field_paths, profile.schema.coercions)


def now() -> float:
    """Module-level so tests can monkeypatch easily."""
    return time.time()
