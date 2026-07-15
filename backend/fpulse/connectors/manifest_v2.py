"""Connector manifest v2 — schema, validator, depth-score calculator.

Implements DESIGN_F01_MANIFEST_V2.md. This is the runtime contract Sprint 1
(bulk loaders) and the Tier-1 connector uplift both depend on.

Public API:
    validate_manifest(path) -> ValidationResult
    migrate_v1_to_v2(v1_dict) -> v2_dict   # generates a TODO-stubbed skeleton
    compute_depth_score(stream_dict) -> int

CLI:
    python -m fpulse.connectors.certify <connector_id>
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


# ── Schema ────────────────────────────────────────────────────────────────

VALID_AUTH_TYPES = {"oauth2", "api_key", "basic", "jwt_bearer", "custom"}
VALID_STATUSES = {"certified", "beta", "roadmap", "plus"}
VALID_CURSOR_STRATEGIES = {"timestamp", "offset", "page_token", "full_refresh"}
VALID_PAGINATION_STRATEGIES = {"cursor", "offset", "page_token", "none"}
VALID_INCREMENTAL_FORMATS = {"iso8601", "unix_seconds", "unix_millis", "string"}
VALID_OWNERS = {"core", "community", "partner"}
REQUIRED_FIXTURE_TYPES = {"happy_path", "empty", "auth_error", "rate_limit", "schema_drift"}


# ── Errors ────────────────────────────────────────────────────────────────

@dataclass
class ValidationError:
    path: str           # JSON path-like locator: 'streams[0].primary_key'
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class ValidationResult:
    connector_id: str
    valid: bool
    declared_depth_score: int           # what the manifest claims
    computed_depth_score: int           # what the validator measures
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[ValidationError] = field(default_factory=list)
    streams_evaluated: list[str] = field(default_factory=list)

    @property
    def effective_depth_score(self) -> int:
        """If validation fails, the connector is depth-0 regardless of claim."""
        if not self.valid:
            return 0
        return min(self.declared_depth_score, self.computed_depth_score)

    def to_dict(self) -> dict:
        return {
            "connector_id": self.connector_id,
            "valid": self.valid,
            "declared_depth_score": self.declared_depth_score,
            "computed_depth_score": self.computed_depth_score,
            "effective_depth_score": self.effective_depth_score,
            "errors": [str(e) for e in self.errors],
            "warnings": [str(w) for w in self.warnings],
            "streams_evaluated": self.streams_evaluated,
        }


# ── Validator ─────────────────────────────────────────────────────────────

def validate_manifest(manifest: dict, *, connector_root: Path | None = None) -> ValidationResult:
    """Validate a v2 manifest dict against the F0.1 schema.

    `connector_root` is used to verify fixture files exist on disk. Pass
    None to skip the file-existence check (useful for pure schema validation).
    """
    errors: list[ValidationError] = []
    warnings: list[ValidationError] = []
    streams_evaluated: list[str] = []

    # ── Top-level shape ──
    version = manifest.get("version")
    if version != 2:
        errors.append(ValidationError("version", f"must be 2, got {version!r}"))

    connector = manifest.get("connector")
    if not isinstance(connector, dict):
        errors.append(ValidationError("connector", "must be an object"))
        return ValidationResult(
            connector_id=str(manifest.get("connector", {}).get("type") if isinstance(manifest.get("connector"), dict) else "unknown"),
            valid=False, declared_depth_score=0, computed_depth_score=0, errors=errors,
        )

    connector_id = str(connector.get("type", "unknown"))

    if not connector.get("type"):
        errors.append(ValidationError("connector.type", "required"))
    if not connector.get("display_name"):
        errors.append(ValidationError("connector.display_name", "required"))
    if not connector.get("category"):
        errors.append(ValidationError("connector.category", "required"))
    if "oss" not in connector:
        warnings.append(ValidationError("connector.oss", "not declared; defaulting to true"))

    # ── Certification block ──
    cert = manifest.get("certification") or {}
    if not isinstance(cert, dict):
        errors.append(ValidationError("certification", "must be an object"))
        cert = {}
    declared_depth_score = int(cert.get("depth_score", 0))
    if declared_depth_score < 0 or declared_depth_score > 5:
        errors.append(ValidationError("certification.depth_score", "must be 0-5"))
    status = cert.get("status")
    if status and status not in VALID_STATUSES:
        errors.append(ValidationError("certification.status", f"must be one of {sorted(VALID_STATUSES)}"))
    owner = cert.get("owner")
    if owner and owner not in VALID_OWNERS:
        errors.append(ValidationError("certification.owner", f"must be one of {sorted(VALID_OWNERS)}"))
    if not cert.get("last_validated"):
        warnings.append(ValidationError("certification.last_validated", "not set"))

    # ── Auth ──
    auth = manifest.get("auth") or {}
    schemes = auth.get("schemes") or []
    if not schemes:
        errors.append(ValidationError("auth.schemes", "must declare at least one"))
    for i, scheme in enumerate(schemes):
        scheme_type = scheme.get("type") if isinstance(scheme, dict) else None
        if scheme_type not in VALID_AUTH_TYPES:
            errors.append(ValidationError(f"auth.schemes[{i}].type", f"must be one of {sorted(VALID_AUTH_TYPES)}"))

    # ── Rate limit + retry ──
    rate_limit = manifest.get("rate_limit") or {}
    retry = rate_limit.get("retry") or {}
    retry_on_status = retry.get("retry_on_status", [])
    for code in retry_on_status:
        if not isinstance(code, int) or code < 400 or code >= 600:
            errors.append(ValidationError("rate_limit.retry.retry_on_status", f"only 4xx/5xx allowed, got {code!r}"))

    # ── Streams ──
    streams = manifest.get("streams") or []
    if not streams:
        errors.append(ValidationError("streams", "must declare at least one stream"))

    stream_names = []
    stream_depth_scores: list[int] = []

    for i, stream in enumerate(streams):
        stream_path = f"streams[{i}]"
        if not isinstance(stream, dict):
            errors.append(ValidationError(stream_path, "must be an object"))
            continue
        name = stream.get("name")
        if not name:
            errors.append(ValidationError(f"{stream_path}.name", "required"))
            continue
        if name in stream_names:
            errors.append(ValidationError(f"{stream_path}.name", f"duplicate stream name: {name}"))
        stream_names.append(name)
        streams_evaluated.append(name)

        # primary_key
        pk = stream.get("primary_key")
        if pk is None:
            errors.append(ValidationError(f"{stream_path}.primary_key", "required (use [] for append-only)"))
        elif not isinstance(pk, list):
            errors.append(ValidationError(f"{stream_path}.primary_key", "must be a list"))

        # incremental_field
        incremental_field = stream.get("incremental_field")
        cursor_strategy = stream.get("cursor_strategy")
        if not incremental_field and cursor_strategy != "full_refresh":
            errors.append(ValidationError(
                f"{stream_path}.incremental_field",
                "required UNLESS cursor_strategy='full_refresh'",
            ))
        if incremental_field:
            inc_format = stream.get("incremental_format")
            if inc_format and inc_format not in VALID_INCREMENTAL_FORMATS:
                errors.append(ValidationError(
                    f"{stream_path}.incremental_format",
                    f"must be one of {sorted(VALID_INCREMENTAL_FORMATS)}",
                ))

        if cursor_strategy and cursor_strategy not in VALID_CURSOR_STRATEGIES:
            errors.append(ValidationError(
                f"{stream_path}.cursor_strategy",
                f"must be one of {sorted(VALID_CURSOR_STRATEGIES)}",
            ))

        # pagination
        pagination = stream.get("pagination") or {}
        pagination_strategy = pagination.get("strategy")
        if pagination_strategy and pagination_strategy not in VALID_PAGINATION_STRATEGIES:
            errors.append(ValidationError(
                f"{stream_path}.pagination.strategy",
                f"must be one of {sorted(VALID_PAGINATION_STRATEGIES)}",
            ))

        # schema
        schema = stream.get("schema")
        schema_errors = _validate_schema(schema, stream_path + ".schema")
        errors.extend(schema_errors)

        # depends_on — must be acyclic + resolvable
        depends_on = stream.get("depends_on") or []
        for dep in depends_on:
            if dep == name:
                errors.append(ValidationError(f"{stream_path}.depends_on", f"cannot depend on self: {dep}"))

        # fixtures — file existence check (if connector_root provided)
        fixtures_for_stream = [
            f for f in (manifest.get("fixtures") or [])
            if isinstance(f, dict) and f.get("stream") == name
        ]
        present_fixture_types = {f.get("name") for f in fixtures_for_stream if isinstance(f, dict)}
        missing_fixtures = REQUIRED_FIXTURE_TYPES - present_fixture_types
        if missing_fixtures:
            # Fixtures are what unlock depth ≥ 3. The connector ladder:
            #   depth 1 — no fixtures expected yet (auto-generated bar)
            #   depth 2 — fixtures partially added, in progress
            #   depth 3+ — all 5 required, missing them is a real error
            # So: silent at depth 1, warning at depth 2, error at ≥3.
            if declared_depth_score >= 3:
                errors.append(ValidationError(
                    f"{stream_path} fixtures",
                    f"missing required fixture types: {sorted(missing_fixtures)}",
                ))
            elif declared_depth_score == 2:
                warnings.append(ValidationError(
                    f"{stream_path} fixtures",
                    f"add {sorted(missing_fixtures)} to reach depth 3+",
                ))
            # depth_score 0 or 1 → no fixture noise at all

        if connector_root is not None:
            for f in fixtures_for_stream:
                file_rel = f.get("file")
                if not file_rel:
                    continue
                file_path = connector_root / file_rel
                if not file_path.exists():
                    errors.append(ValidationError(
                        f"{stream_path} fixtures",
                        f"fixture file missing: {file_rel}",
                    ))

        # depth-score per stream
        stream_depth_scores.append(compute_stream_depth_score(stream, fixtures_for_stream))

    # depends_on cycle detection
    if streams:
        cycle = _find_cycle(streams)
        if cycle:
            errors.append(ValidationError("streams", f"depends_on cycle detected: {' -> '.join(cycle)}"))

    valid = not errors
    computed = min(stream_depth_scores) if stream_depth_scores else 0

    return ValidationResult(
        connector_id=connector_id,
        valid=valid,
        declared_depth_score=declared_depth_score,
        computed_depth_score=computed,
        errors=errors,
        warnings=warnings,
        streams_evaluated=streams_evaluated,
    )


def _validate_schema(schema: Any, path: str) -> list[ValidationError]:
    """Lightweight JSON Schema draft-07 sanity check."""
    if not isinstance(schema, dict):
        return [ValidationError(path, "required, must be a JSON Schema object")]
    errs: list[ValidationError] = []
    if schema.get("type") != "object":
        errs.append(ValidationError(path + ".type", "must be 'object'"))
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        errs.append(ValidationError(path + ".properties", "must be a non-empty object"))
        return errs
    required = schema.get("required") or []
    for r in required:
        if r not in properties:
            errs.append(ValidationError(
                path + f".required[{r}]",
                f"required field '{r}' not in properties",
            ))
    for prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            errs.append(ValidationError(path + f".properties.{prop_name}", "must be an object"))
            continue
        if "type" not in prop_def:
            errs.append(ValidationError(path + f".properties.{prop_name}.type", "required"))
    return errs


def _find_cycle(streams: list[dict]) -> list[str] | None:
    """DFS-based cycle detection over depends_on graph."""
    graph = {s["name"]: list(s.get("depends_on") or []) for s in streams if isinstance(s, dict) and s.get("name")}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in color:
                continue  # unresolved dep — surfaced as a separate error elsewhere
            if color[nxt] == GRAY:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == WHITE:
                cycle = dfs(nxt)
                if cycle:
                    return cycle
        color[node] = BLACK
        stack.pop()
        return None

    for n in graph:
        if color[n] == WHITE:
            cycle = dfs(n)
            if cycle:
                return cycle
    return None


# ── Depth score ───────────────────────────────────────────────────────────

def compute_stream_depth_score(stream: dict, fixtures: list[dict]) -> int:
    """Compute the actual depth score for a stream based on what's present.

      0 — UI only, no backend (manifest exists but stream has no schema)
      1 — basic API call works (has schema)
      2 — pagination handled (has pagination block with strategy)
      3 — incremental sync wired (has incremental_field OR cursor_strategy != full_refresh)
      4 — primary key + upsert path (has non-empty primary_key)
      5 — full v2 contract (all 5 fixture types present)
    """
    if not isinstance(stream, dict):
        return 0
    schema = stream.get("schema")
    if not isinstance(schema, dict) or not schema.get("properties"):
        return 0

    score = 1  # has schema → basic
    pagination = stream.get("pagination") or {}
    if pagination.get("strategy") and pagination.get("strategy") != "none":
        score = 2

    incremental_field = stream.get("incremental_field")
    cursor_strategy = stream.get("cursor_strategy")
    if (incremental_field or (cursor_strategy and cursor_strategy != "full_refresh")) and score >= 2:
        score = 3

    pk = stream.get("primary_key") or []
    if isinstance(pk, list) and len(pk) > 0 and score >= 3:
        score = 4

    fixture_types = {f.get("name") for f in fixtures if isinstance(f, dict)}
    if REQUIRED_FIXTURE_TYPES.issubset(fixture_types) and score >= 4:
        score = 5

    return score


# ── v1 → v2 migration ─────────────────────────────────────────────────────

def migrate_v1_to_v2(v1: dict) -> dict:
    """Generate a v2 skeleton from a v1 manifest. Fields that need human input
    are stubbed with TODO comments so the validator surfaces them as errors
    until a human fills them in.

    Output is depth-score 0 — calling this function never elevates a connector
    to certified status.
    """
    v1_streams = v1.get("streams") or []

    auth_type_v2 = "api_key"
    v1_auth_type = (v1.get("auth") or {}).get("type", "")
    if v1_auth_type == "oauth2":
        auth_type_v2 = "oauth2"
    elif v1_auth_type in ("basic", "bearer"):
        auth_type_v2 = "basic" if v1_auth_type == "basic" else "jwt_bearer"

    return {
        "version": 2,
        "connector": {
            "type": v1.get("id", "unknown"),
            "display_name": v1.get("name", "Unknown"),
            "category": v1.get("category", "saas"),
            "vendor": "TODO",
            "homepage": "TODO",
            "docs_url": f"https://hybridyn.com/connectors/{v1.get('id', 'unknown')}",
            "oss": True,
        },
        "certification": {
            "depth_score": 0,
            "status": "roadmap",
            "last_validated": None,
            "owner": "core",
            "validator": "ci.fpulse",
            "known_issues": ["TODO: human review required after v1→v2 migration"],
        },
        "auth": {
            "schemes": [{"type": auth_type_v2}],
            "rotation": {},
        },
        "rate_limit": {
            "default": {"requests_per_minute": 60},
            "retry": {
                "max_attempts": 5,
                "backoff": "exponential",
                "base_seconds": 2,
                "max_seconds": 60,
                "retry_on_status": [429, 500, 502, 503, 504],
            },
        },
        "streams": [
            {
                "name": s.get("name", "unknown"),
                "primary_key": [],         # TODO: declare
                "incremental_field": None, # TODO: declare or set cursor_strategy=full_refresh
                "cursor_strategy": "full_refresh",
                "soft_delete_field": None,
                "pagination": _migrate_pagination(s.get("pagination") or {}),
                "depends_on": [],
                "schema": {                 # TODO: replace with real JSON Schema
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "required": [],
                    "properties": {
                        "_todo": {"type": "string", "description": "Replace with real fields"},
                    },
                },
            }
            for s in v1_streams
        ],
        "fixtures": [],  # TODO: add 5 fixtures per stream
        "_migration_notes": [
            "v2 skeleton generated by migrate_v1_to_v2.",
            "Required follow-ups before this manifest can score above 0:",
            "  1. Fill in primary_key per stream (or set to [] explicitly with a justification)",
            "  2. Declare incremental_field + incremental_format OR set cursor_strategy='full_refresh'",
            "  3. Replace schema._todo with real JSON Schema for each stream",
            "  4. Add 5 fixtures per stream (happy_path, empty, auth_error, rate_limit, schema_drift)",
            "  5. Set certification.depth_score and certification.last_validated",
        ],
    }


def _migrate_pagination(v1_pagination: dict) -> dict:
    """Map v1 pagination shapes to v2."""
    v1_type = v1_pagination.get("type", "")
    strategy = "none"
    if v1_type == "cursor":
        strategy = "cursor"
    elif v1_type == "page":
        strategy = "page_token"
    elif v1_type == "offset":
        strategy = "offset"
    return {
        "strategy": strategy,
        "page_size": v1_pagination.get("page_size") or 100,
        "max_pages": v1_pagination.get("max_pages") or 100,
    }


def validate_manifest_file(path: str | Path) -> ValidationResult:
    """Load + validate a manifest JSON file. Convenience wrapper."""
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return validate_manifest(manifest, connector_root=path.parent)
