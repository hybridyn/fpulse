"""
Connector Certification Matrix API — Gate 3 of the launch scorecard.

Surfaces the depth-score for every connector manifest on disk, computed by
the F0.1 validator (`manifest_v2.py`). Lets operators (and prospects) see
which connectors are production-grade vs. beta vs. alpha — published pass
rates per the positioning lock's Gate 3.

Endpoints:
  GET /api/connectors/cert-matrix
    Returns one row per manifest with:
      - id (e.g. 'salesforce')
      - display_name
      - category (e.g. 'crm', 'finance', 'support')
      - vendor
      - manifest_version (1 or 2)
      - depth_score (0–5)            — stream-level max (v2 only; v1 stays 0)
      - depth_label                   — for v2: 'production' | 'beta' | 'alpha' | 'stub'
                                        for v1: 'v1-functional' | 'v1-basic' | 'v1-stub'
      - validation_status: 'pass' | 'fail' | 'unvalidated' | 'uncertified'
                          ('uncertified' = v1 manifest, not on F0.1 cert path,
                           may still be functional at runtime — see v1_capability_score)
      - v1_capability_score (v1 only) — 0..3 from auth(1)+streams(1)+pagination(1)
      - issues_count: int            — number of validator errors
      - streams_count: int
      - audited_at: ISO timestamp    — when this matrix was last computed
    Plus a top-level summary:
      - total: int
      - by_label: { production: N, beta: N, alpha: N, stub: N }
      - last_audited: ISO timestamp

  GET /api/connectors/cert-matrix/{connector_id}
    Single-connector detail — full ValidationResult + per-stream depth
    breakdown. Useful for the connector detail page.

Design choices:
  * Depth-score scanning is cheap (~few ms per manifest) so we compute on
    every request rather than caching. Add an etag-based cache later if
    the manifest set grows beyond ~100.
  * v1 manifests are reported with `validation_status: "uncertified"` and a
    `v1_capability_score` (0..3) reflecting the functional shape (auth
    declared, streams declared, pagination wired). depth_score stays 0
    (only v2 is on the F0.1 cert path), but the depth_label distinguishes
    `v1-functional` from `v1-stub` so operators don't confuse a working
    HubSpot manifest with an empty placeholder. A `migration_hint` field
    points at the certify CLI for the v2 migration path.
  * Public endpoint (no auth required) — the cert matrix is part of the
    Gate 4 trust artifact bundle and prospects need to see it pre-signup.
    Fine because it only reveals what's already shipped on the box.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from fpulse.connectors.manifest_v2 import (
    compute_stream_depth_score,
    validate_manifest,
    validate_manifest_file,
)

router = APIRouter(prefix="/api/connectors", tags=["connectors"])
logger = logging.getLogger(__name__)


# ── Module-level config ──────────────────────────────────────────────


def _manifests_dir() -> Path:
    """Resolve the manifests directory regardless of cwd. The path is
    fixed relative to the package, so we look it up at request time
    rather than import time (lets a test fixture monkeypatch the
    package layout if needed)."""
    import fpulse.connectors as connectors_pkg
    return Path(connectors_pkg.__file__).parent / "manifests"


_DEPTH_LABELS = {
    0: "stub",
    1: "alpha",
    2: "alpha",
    3: "beta",
    4: "beta",
    5: "production",
}


def _depth_label(score: int) -> str:
    return _DEPTH_LABELS.get(score, "unknown")


# v1 capability tiers — for manifests that haven't migrated to F0.1 v2 but
# are functional at runtime. Distinguishes a working v1 manifest from a
# truly empty stub. Map maps capability_score → label; the score itself is
# auth(1) + streams(1) + pagination(1) = 0..3.
_V1_CAPABILITY_LABELS = {
    0: "v1-stub",
    1: "v1-basic",
    2: "v1-basic",
    3: "v1-functional",
}


def _v1_capability_score(manifest: dict[str, Any]) -> int:
    """Score a v1 manifest's runtime capabilities on a 0-3 scale.

    +1 if `auth` is declared (any non-empty dict)
    +1 if at least one stream is declared with a non-empty `path`
    +1 if at least one stream declares pagination

    Used for honest cert-matrix labeling — a v1 HubSpot manifest with
    bearer auth + 4 paginated streams scores 3 ("v1-functional"), and
    should not be presented to operators as a "stub" alongside an empty
    placeholder file. v1 manifests are still uncertified by the F0.1
    validator (depth_score stays 0); this is a parallel signal of
    "does it work today."
    """
    score = 0
    auth = manifest.get("auth")
    if isinstance(auth, dict) and auth:
        score += 1
    streams = manifest.get("streams") or []
    if isinstance(streams, list):
        non_empty = [s for s in streams if isinstance(s, dict) and s.get("path")]
        if non_empty:
            score += 1
            if any(isinstance(s.get("pagination"), dict) and s.get("pagination") for s in non_empty):
                score += 1
    return score


def _v1_capability_label(score: int) -> str:
    return _V1_CAPABILITY_LABELS.get(score, "v1-stub")


# ── Tier vocabulary (2026-06-02) ─────────────────────────────────────
#
# Five-tier user-facing maturity classification. Tier vocabulary
# (certified / community / experimental / hidden) groups connectors by
# verification confidence. The cert matrix
# already exposes `depth_label` and `v1-functional/basic/stub` —
# this `tier` field is a single user-facing axis derived from those
# plus an optional declared override on the manifest.
#
# A manifest may declare `"tier": "experimental"` to opt DOWN
# (publishing honesty), but cannot opt UP — the computed tier is
# a ceiling. This stops a manifest author from labelling a stub
# manifest "production" by hand.
_VALID_TIERS = {"production", "verified", "beta", "experimental", "hidden"}
_TIER_ORDER = {
    "production": 4,
    "verified": 3,
    "beta": 2,
    "experimental": 1,
    "hidden": 0,
}
_VALID_VISIBILITY = {"public", "hidden"}


def _compute_tier(
    row: dict[str, Any],
    manifest: dict[str, Any],
    *,
    has_smoke_fixture: bool = False,
    in_live_smoke_allowlist: bool = False,
) -> str:
    """Compute the user-facing tier from existing cert-matrix signals.

    Rules (highest match wins; declared `tier` on the manifest can
    only opt DOWN, never UP):

      production    depth_score == 5 AND smoke fixture present AND in CI allow-list
      verified      depth_score >= 3 AND validation_status == "pass" AND issues == 0
                    AND smoke fixture present
      beta          v2 with validation_status == "pass" AND depth_score >= 1
                    OR v1 with capability_score >= 3
      experimental  parses but neither v2-pass nor v1-functional
      hidden        only when explicitly declared by the manifest

    `has_smoke_fixture` reflects whether
    `backend/tests/fixtures/connectors/<id>/smoke.json` exists.
    `in_live_smoke_allowlist` reflects whether the connector is
    listed in `backend/fpulse/connectors/ci/live_smoke.yml`. Both
    default to False; the live-smoke CI workflow updates them.
    """
    depth_score = int(row.get("depth_score", 0))
    validation = str(row.get("validation_status", ""))
    issues = int(row.get("issues_count", 0))
    v1_cap = int(row.get("v1_capability_score", 0))
    version = int(row.get("manifest_version", 1))

    # Compute the natural tier ceiling from signals.
    if (
        depth_score >= 5
        and validation == "pass"
        and issues == 0
        and has_smoke_fixture
        and in_live_smoke_allowlist
    ):
        computed = "production"
    elif (
        depth_score >= 3
        and validation == "pass"
        and issues == 0
        and has_smoke_fixture
    ):
        computed = "verified"
    elif (version >= 2 and validation == "pass" and depth_score >= 1) or (
        version < 2 and v1_cap >= 3
    ):
        computed = "beta"
    else:
        computed = "experimental"

    # Honour manifest-declared tier, but only as a CEILING-CAP downward.
    # `hidden` is special — it's the manifest's way of saying "do not
    # show in public listings". Always honoured.
    declared = manifest.get("tier")
    if isinstance(declared, str):
        declared = declared.lower().strip()
        if declared == "hidden":
            return "hidden"
        if declared in _VALID_TIERS and _TIER_ORDER[declared] <= _TIER_ORDER[computed]:
            return declared

    # `visibility: "hidden"` also forces hidden tier (separate axis;
    # one flag is enough to take the row out of public listings).
    visibility = manifest.get("visibility")
    if isinstance(visibility, str) and visibility.lower() == "hidden":
        return "hidden"

    # 2026-06-02 back-compat: three manifests (google_ads, linkedin_ads,
    # facebook_ads) carried a top-level boolean `"hidden": true` flag
    # before the tier system existed. No code was reading it. Honour
    # it as equivalent to `tier: hidden` so the existing curator
    # intent isn't lost.
    if manifest.get("hidden") is True:
        return "hidden"

    return computed


def _has_smoke_fixture(connector_id: str) -> bool:
    """True iff `backend/tests/fixtures/connectors/<id>/smoke.json` exists.

    The fixture file is what the Verified tier requires alongside the
    cert-matrix signals. It contains: sample params, expected response
    shapes per stream, and any HTTP fixture cassettes for reproducible
    smoke runs. Absence is informative — most v1 manifests don't have
    one yet and that's exactly why they're capped at Beta.
    """
    fixture_path = (
        _manifests_dir().parent.parent.parent
        / "tests"
        / "fixtures"
        / "connectors"
        / connector_id
        / "smoke.json"
    )
    return fixture_path.is_file()


def _live_smoke_allowlist() -> set[str]:
    """Return the set of connector ids approved for CI live-smoke runs.

    Read from `backend/fpulse/connectors/ci/live_smoke.yml`. The file
    lists connectors that have credentials in GitHub Secrets and are
    safe to call against the real vendor in CI. Absence of the file
    means "no connector is in live-smoke yet" — a fine default for
    a fresh install.
    """
    allowlist_path = (
        _manifests_dir().parent / "ci" / "live_smoke.yml"
    )
    if not allowlist_path.is_file():
        return set()
    try:
        import yaml  # local import — pyyaml is already a core dep
        with open(allowlist_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        entries = data.get("connectors") or []
        return {
            str(e["id"]) for e in entries
            if isinstance(e, dict) and isinstance(e.get("id"), str)
        }
    except Exception:  # noqa: BLE001
        # Don't break the cert matrix if the YAML is malformed — just
        # treat it as empty and let the operator fix the file.
        return set()


# ── Capability / role extraction (#12 step 1 — 2026-05-26) ──────────
#
# The picker UI needs to surface badges next to each connector card:
#   Source / Sink / Action / Trigger        (role chips)
#   Test ✓ / Schema discovery ✓ /            (capability ticks)
#   Pagination ✓ / Incremental ✓
#   Certified / Beta                         (from depth_label, already exposed)
#
# This helper reads what each manifest already declares — no schema
# changes required on the manifest side, and missing fields default to
# False so the matrix never over-promises. Future manifests can add an
# explicit top-level `roles: ["source", "sink", "trigger"]` array to
# override the inferred default.


def _extract_capabilities(manifest: dict[str, Any], version: int) -> dict[str, Any]:
    """Read capability + role flags from a manifest for cert-matrix badges.

    Returns a dict with two keys:
        "roles":        list[str]   — subset of [source, sink, action, trigger]
        "capabilities": dict[str, bool] — granular feature flags

    Honest defaults: when in doubt, return False. The picker is allowed
    to say "we don't know" but it must not falsely claim incremental /
    schema-discovery support that isn't actually wired in the manifest.
    """
    streams = manifest.get("streams") or []
    if not isinstance(streams, list):
        streams = []

    def _stream_field(s: Any, *keys: str) -> Any:
        if not isinstance(s, dict):
            return None
        for k in keys:
            v = s.get(k)
            if v:
                return v
        return None

    has_streams = any(_stream_field(s, "path", "name") for s in streams)
    has_auth = isinstance(manifest.get("auth"), dict) and bool(manifest.get("auth"))

    # Pagination — any stream declares a non-empty pagination config.
    has_pagination = any(
        isinstance(s, dict)
        and isinstance(s.get("pagination"), dict)
        and bool(s.get("pagination"))
        for s in streams
    )

    # Incremental — covers both v1 ("incremental_cursor" / "cursor_field")
    # and v2 ("incremental_field" + "cursor_strategy") shapes.
    def _stream_incremental(s: Any) -> bool:
        if not isinstance(s, dict):
            return False
        if s.get("incremental_field") or s.get("incremental_cursor"):
            return True
        if s.get("cursor_strategy") in {"timestamp", "id"}:
            return True
        inc = s.get("incremental")
        if isinstance(inc, dict) and inc:
            return True
        return False

    has_incremental = any(_stream_incremental(s) for s in streams)

    # Schema discovery — only meaningful on v2 manifests where streams
    # declare primary_key / fields / schema explicitly. v1 manifests
    # discover schema implicitly at runtime from the response, which
    # doesn't count as "schema discovery declared" by our standard.
    has_schema = False
    if version >= 2:
        has_schema = any(
            isinstance(s, dict)
            and (s.get("primary_key") or s.get("fields") or s.get("schema"))
            for s in streams
        )

    # Test — the picker's "Test connection" button works if the manifest
    # has at least one stream path AND declared auth (so the backend can
    # build a real request). v1 manifests with auth + streams pass.
    has_test = has_streams and has_auth

    # 2026-05-30 (P3 expansion): four additional capability flags the
    # cert matrix UI now surfaces. Detection rules below default to
    # False — the matrix must not over-promise.
    #
    # oauth_refresh: auth block declares oauth2 with refresh_url or
    #   a token-renewal hook. Without this, OAuth connectors silently
    #   drop dead when the access token expires mid-run.
    auth_block = manifest.get("auth") if isinstance(manifest.get("auth"), dict) else {}
    auth_type = str(auth_block.get("type", "")).lower()
    has_oauth_refresh = (
        auth_type in {"oauth2", "oauth"}
        and bool(
            auth_block.get("refresh_url")
            or auth_block.get("refresh_token_url")
            or auth_block.get("refresh")
        )
    )

    # rate_limit: any stream OR top-level declares a retry / backoff /
    #   throttle policy. Connectors without this slam APIs and get
    #   blacklisted in production.
    top_rate = manifest.get("rate_limit") or manifest.get("retry")
    has_rate_limit = bool(top_rate) or any(
        isinstance(s, dict)
        and bool(s.get("rate_limit") or s.get("retry") or s.get("backoff"))
        for s in streams
    )

    # schema_drift: manifest declares a drift policy (strict / additive /
    #   open) at the connector or stream level. Without this, schema
    #   changes upstream break the pipeline silently.
    top_drift = manifest.get("schema_drift") or manifest.get("schema_policy")
    has_schema_drift = bool(top_drift) or any(
        isinstance(s, dict)
        and bool(s.get("schema_drift") or s.get("schema_policy"))
        for s in streams
    )

    # backfill_safety: connector explicitly declares whether re-running
    #   a window is safe (idempotent), risky (duplicates), or external
    #   (side-effect-bearing). Surfaced on the Backfill UI before launch.
    declared_safety = manifest.get("backfill_safety")
    if isinstance(declared_safety, str) and declared_safety:
        has_backfill_safety = True
    else:
        # Inferred: has primary_key + incremental_field → safe to backfill
        # (re-runs upsert deterministically). Anything else stays False.
        has_backfill_safety = bool(has_incremental) and any(
            isinstance(s, dict) and s.get("primary_key") for s in streams
        )

    # Roles — explicit `roles: ["source", "sink", ...]` overrides; default
    # to "source" when the manifest has streams (every existing OSS
    # manifest is a read-side connector). Sink / action / trigger roles
    # are reserved for future manifests that explicitly declare them.
    declared_roles = manifest.get("roles")
    if isinstance(declared_roles, list) and declared_roles:
        roles: list[str] = [
            r for r in declared_roles
            if isinstance(r, str) and r in {"source", "sink", "action", "trigger"}
        ]
    else:
        roles = ["source"] if has_streams else []

    return {
        "roles": roles,
        "capabilities": {
            "source": "source" in roles,
            "sink": "sink" in roles,
            "action": "action" in roles,
            "trigger": "trigger" in roles,
            "pagination": has_pagination,
            "incremental": has_incremental,
            "schema": has_schema,
            "test": has_test,
            # 2026-05-30 (P3) — explicit additions:
            "oauth_refresh": has_oauth_refresh,
            "rate_limit": has_rate_limit,
            "schema_drift": has_schema_drift,
            "backfill_safety": has_backfill_safety,
        },
    }


# ── Per-manifest summary ─────────────────────────────────────────────


def _summarize_manifest(path: Path) -> dict[str, Any]:
    """Return the cert-matrix row for a single manifest file. Errors
    while reading are reported as a 'fail' row so the matrix surfaces
    every manifest, not just the well-formed ones."""
    row: dict[str, Any] = {
        "id": path.stem,
        "display_name": path.stem.replace("_", " ").title(),
        "category": "uncategorized",
        "vendor": "",
        "manifest_version": 1,
        "depth_score": 0,
        "depth_label": "stub",
        "validation_status": "unvalidated",
        "issues_count": 0,
        "streams_count": 0,
        "manifest_path": str(path.relative_to(_manifests_dir().parent)) if path.is_relative_to(_manifests_dir().parent) else path.name,
    }

    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as exc:  # noqa: BLE001
        row["validation_status"] = "fail"
        row["issues_count"] = 1
        row["last_error"] = f"Could not read manifest: {exc}"
        return row

    # Detect manifest version. v2 has top-level "version": 2; v1 doesn't.
    version = int(manifest.get("version", 1))
    row["manifest_version"] = version

    # Display metadata (best-effort across both versions).
    if version >= 2:
        connector = manifest.get("connector") or {}
        row["display_name"] = connector.get("display_name") or row["display_name"]
        row["category"] = connector.get("category") or row["category"]
        row["vendor"] = connector.get("vendor") or row["vendor"]
    else:
        row["display_name"] = manifest.get("display_name") or row["display_name"]
        row["category"] = manifest.get("category") or row["category"]
        row["vendor"] = manifest.get("vendor") or row["vendor"]

    streams = manifest.get("streams") or []
    row["streams_count"] = len(streams) if isinstance(streams, list) else 0

    # #12 step 1 — capability + role flags for the picker UI. Both v1
    # and v2 manifests get this — v1 manifests will show "source" + the
    # capabilities that can be inferred (pagination, sometimes
    # incremental); v2 manifests get full coverage including schema.
    caps = _extract_capabilities(manifest, version)
    row["roles"] = caps["roles"]
    row["capabilities"] = caps["capabilities"]

    # 2026-05-30 (P3): known_gaps surfaces curated honesty per
    # connector — what an operator must know BEFORE building a pipeline
    # against this connector. The list is the union of:
    #   (a) manifest-declared `known_gaps: ["..."]` (curator notes)
    #   (b) auto-inferred gaps from the capabilities above (e.g.
    #       "no rate-limit handling declared")
    # Either or both can be empty. The UI renders this list in the
    # connector detail expand panel beside the capability ticks.
    declared_gaps = manifest.get("known_gaps")
    known_gaps: list[str] = []
    if isinstance(declared_gaps, list):
        known_gaps.extend(str(g) for g in declared_gaps if g)
    # Auto-inferred gaps — only for v2 manifests where we expect full
    # coverage. v1 manifests are honestly labeled "uncertified" already.
    if version >= 2:
        if not caps["capabilities"]["rate_limit"]:
            known_gaps.append("No rate-limit / backoff policy declared — connector may hit API throttles on high-volume runs.")
        if not caps["capabilities"]["oauth_refresh"] and isinstance(manifest.get("auth"), dict) and str(manifest["auth"].get("type", "")).lower() in {"oauth", "oauth2"}:
            known_gaps.append("OAuth without refresh token handling — pipelines stop working when the access token expires.")
        if not caps["capabilities"]["schema_drift"]:
            known_gaps.append("No schema-drift policy — upstream column add/remove breaks downstream transforms.")
        if not caps["capabilities"]["backfill_safety"]:
            known_gaps.append("Backfill safety not verified — re-running a window may produce duplicates.")
    row["known_gaps"] = known_gaps

    if version < 2:
        # v1 manifests aren't on the F0.1 cert path — depth_score stays 0
        # (the validator only certifies v2). But "stub" + "fail" was misleading
        # for working manifests — a v1 HubSpot with bearer auth + 4 paginated
        # streams is functional at runtime even though it hasn't been migrated.
        # Compute a parallel v1-capability signal so the operator sees the
        # difference between a truly empty placeholder and a fully-wired v1.
        cap_score = _v1_capability_score(manifest)
        row["validation_status"] = "uncertified"
        row["depth_score"] = 0
        row["depth_label"] = _v1_capability_label(cap_score)
        row["v1_capability_score"] = cap_score
        row["issues_count"] = 0
        row["migration_hint"] = (
            f"Run `python -m fpulse.connectors.certify --migrate {path.stem}` "
            "to scaffold a v2 version on the F0.1 cert path."
        )
        # 2026-06-02: tier + visibility (user-facing maturity label).
        # Derived from the v1 capability score, optionally capped by a
        # manifest-declared `tier` / `visibility` flag.
        row["tier"] = _compute_tier(
            row, manifest,
            has_smoke_fixture=_has_smoke_fixture(path.stem),
            in_live_smoke_allowlist=path.stem in _LIVE_SMOKE_ALLOWLIST_CACHE,
        )
        row["visibility"] = (
            "hidden" if str(manifest.get("visibility", "")).lower() == "hidden"
            or row["tier"] == "hidden"
            else "public"
        )
        return row

    # v2 — run the validator.
    try:
        result = validate_manifest(manifest, connector_root=path.parent)
        row["validation_status"] = "pass" if result.valid else "fail"
        row["issues_count"] = len(result.errors) if hasattr(result, "errors") else 0
        if hasattr(result, "errors") and result.errors:
            row["last_error"] = str(result.errors[0])
        # The validator already computes a depth score for the whole
        # connector via `effective_depth_score` (= min(declared, computed)
        # if valid, else 0). Use that as the canonical row score so the
        # matrix and `validate_manifest_file` always agree.
        row["depth_score"] = int(getattr(result, "effective_depth_score",
                                         result.computed_depth_score))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cert-matrix: validation crashed for %s: %s", path.stem, exc)
        row["validation_status"] = "fail"
        row["issues_count"] = 1
        row["last_error"] = f"Validator crashed: {exc}"
        return row

    # Cross-check against per-stream computation. If the validator
    # computed a connector-wide score of 0 but at least one stream
    # actually scores higher (e.g. a future v2 manifest with a partial
    # fixture set), surface the per-stream max so the matrix doesn't
    # under-report a partial-coverage connector. Production grade still
    # requires depth==5 + valid==true; this only changes how the matrix
    # presents alpha/beta tiers.
    if isinstance(streams, list) and streams and row["depth_score"] == 0 and row["validation_status"] == "fail":
        per_stream_scores: list[int] = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            fixtures = stream.get("fixtures") or []
            if not isinstance(fixtures, list):
                fixtures = []
            try:
                per_stream_scores.append(compute_stream_depth_score(stream, fixtures))
            except Exception:  # noqa: BLE001
                continue
        if per_stream_scores:
            row["depth_score"] = max(per_stream_scores)

    row["depth_label"] = _depth_label(int(row["depth_score"]))
    # 2026-06-02: tier + visibility for v2 manifests too. Verified
    # requires both depth-score signals AND a smoke fixture on disk;
    # Production additionally requires inclusion in the live-smoke
    # allow-list (so it actually runs against the vendor in CI).
    row["tier"] = _compute_tier(
        row, manifest,
        has_smoke_fixture=_has_smoke_fixture(path.stem.replace(".v2", "")),
        in_live_smoke_allowlist=path.stem.replace(".v2", "") in _LIVE_SMOKE_ALLOWLIST_CACHE,
    )
    row["visibility"] = (
        "hidden" if str(manifest.get("visibility", "")).lower() == "hidden"
        or row["tier"] == "hidden"
        else "public"
    )
    return row


# ── Endpoints ────────────────────────────────────────────────────────


# Cached at module load — refreshed by `cert_matrix()` on each call so a
# CI-committed update to live_smoke.yml is reflected without a restart.
_LIVE_SMOKE_ALLOWLIST_CACHE: set[str] = set()


@router.get("/cert-matrix")
def cert_matrix(include_hidden: bool = False) -> dict[str, Any]:
    """Return the connector certification matrix.

    Hidden connectors (manifest declares `visibility: "hidden"` or
    `tier: "hidden"`) are filtered from the default listing — they
    stay in the file tree so slugs are reserved + links don't 404,
    but they don't show in pickers or marketing surfaces. Pass
    `?include_hidden=true` to retrieve them too (admin / debug only).

    Stable shape:
        {
          "audited_at": "<iso>",
          "total": <int>,                                  # visible rows only
          "by_label": { "stub": N, "alpha": N, ... },      # depth labels
          "by_tier":  { "production": N, "verified": N,    # 2026-06-02
                        "beta": N, "experimental": N,
                        "hidden": N },
          "by_category": { ... },
          "v2_total": <int>,
          "production_total": <int>,
          "hidden_total": <int>,
          "rows": [ {...row...}, ... ]
        }
    """
    # Refresh the live-smoke allow-list on every call so tier computation
    # uses the most recent CI status (the file is rewritten by the
    # nightly workflow). Module-level cache avoids re-reading the YAML
    # for every row in the same request.
    global _LIVE_SMOKE_ALLOWLIST_CACHE
    _LIVE_SMOKE_ALLOWLIST_CACHE = _live_smoke_allowlist()

    manifests_dir = _manifests_dir()
    rows: list[dict[str, Any]] = []
    if manifests_dir.is_dir():
        # Sort by id for stable output (renders deterministic in the UI).
        # Skip any `.v2.json` siblings of a `.json` v1 manifest — when a
        # v1 file exists alongside a v2 fork, the v2 takes precedence.
        v2_basenames = {p.stem.replace(".v2", "") for p in manifests_dir.glob("*.v2.json")}
        for path in sorted(manifests_dir.glob("*.json")):
            stem = path.stem
            # Skip the v1 file when a `.v2.json` exists for the same name.
            if not stem.endswith(".v2") and stem in v2_basenames:
                continue
            try:
                rows.append(_summarize_manifest(path))
            except Exception as exc:  # noqa: BLE001
                logger.warning("cert-matrix: skip %s (%s)", path.name, exc)

    # Tally over ALL rows (including hidden) for the *_total aggregates,
    # then filter rows for the actual listing.
    by_label: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_category: dict[str, int] = {}
    v2_total = 0
    prod_total = 0
    hidden_total = 0
    for r in rows:
        by_label[r["depth_label"]] = by_label.get(r["depth_label"], 0) + 1
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
        by_category[r["category"]] = by_category.get(r["category"], 0) + 1
        if r["manifest_version"] >= 2:
            v2_total += 1
        if r["depth_score"] >= 5:
            prod_total += 1
        if r["tier"] == "hidden":
            hidden_total += 1

    if not include_hidden:
        rows = [r for r in rows if r["tier"] != "hidden"]

    return {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "total": len(rows),
        "v2_total": v2_total,
        "production_total": prod_total,
        "hidden_total": hidden_total,
        "by_label": by_label,
        "by_tier": by_tier,
        "by_category": by_category,
        "rows": rows,
    }


@router.get("/cert-matrix/{connector_id}")
def cert_matrix_detail(connector_id: str) -> dict[str, Any]:
    """Per-connector deep dive — full validation result + per-stream depth.

    Used by the cert matrix UI's row-expand and by the connector detail page.
    """
    # Validate the id; reject path-traversal up front rather than relying on
    # Path() to fail later.
    if "/" in connector_id or ".." in connector_id or "\\" in connector_id:
        raise HTTPException(status_code=400, detail="invalid connector id")

    manifests_dir = _manifests_dir()
    # Prefer the v2 file if it exists, fall back to v1.
    candidates = [
        manifests_dir / f"{connector_id}.v2.json",
        manifests_dir / f"{connector_id}.json",
    ]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        raise HTTPException(status_code=404, detail=f"connector '{connector_id}' not found")

    summary = _summarize_manifest(path)

    # Per-stream depth breakdown (v2 only).
    per_stream: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("could not read manifest")
        raise HTTPException(status_code=500, detail="could not read manifest") from exc
    if int(manifest.get("version", 1)) >= 2:
        for stream in manifest.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            fixtures = stream.get("fixtures") or []
            if not isinstance(fixtures, list):
                fixtures = []
            try:
                score = compute_stream_depth_score(stream, fixtures)
            except Exception:  # noqa: BLE001
                score = 0
            per_stream.append({
                "name": stream.get("name", ""),
                "depth_score": score,
                "depth_label": _depth_label(score),
                "has_schema": bool((stream.get("schema") or {}).get("properties")),
                "has_pagination": bool((stream.get("pagination") or {}).get("strategy")),
                "incremental_field": stream.get("incremental_field"),
                "primary_key": stream.get("primary_key") or [],
                "fixture_types": [
                    f.get("name") for f in fixtures
                    if isinstance(f, dict) and f.get("name")
                ],
            })

    # Re-run validation to surface the full error list (summary only had count).
    issues: list[str] = []
    try:
        result = validate_manifest_file(str(path))
        if not result.valid and hasattr(result, "errors"):
            for err in result.errors:
                issues.append(str(err))
    except Exception as exc:  # noqa: BLE001
        issues.append(f"Validator crashed: {exc}")

    return {
        **summary,
        "per_stream": per_stream,
        "issues": issues,
    }
