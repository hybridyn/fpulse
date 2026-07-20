"""Tests for SourceProfile validation + the bundled sample profile.

The profile is the keystone abstraction — a misconfigured profile
should fail at registration, not after a 6-hour run kicks off and
hits an inconsistent state. Validation lives in __post_init__ and
this file locks down each enum and required-field check.
"""

from __future__ import annotations

import pytest

from fpulse.extraction.profile import (
    AuthProfile,
    CheckpointProfile,
    ConcurrencyProfile,
    EnrichmentProfile,
    PaginationProfile,
    RateLimitProfile,
    SchemaProfile,
    SourceProfile,
)


def _minimal_profile(**overrides) -> SourceProfile:
    """Build a minimal but valid profile; overrides patch fields for
    negative-test cases."""
    defaults = dict(
        name="test_source",
        auth=AuthProfile(type="api_token"),
        pagination=PaginationProfile(mode="offset", items_path=["data"]),
        schema=SchemaProfile(field_paths={"id": "id"}),
    )
    defaults.update(overrides)
    return SourceProfile(**defaults)


# ── Happy path ──────────────────────────────────────────────────────

def test_minimal_profile_validates():
    p = _minimal_profile()
    assert p.name == "test_source"
    # Defaults applied.
    assert p.latency_class == "fast"
    assert p.concurrency.mode == "aimd"
    assert p.checkpoint.unit == "per_record"
    assert p.enrichment is None


def test_freshness_interval_carries_through():
    p = _minimal_profile(freshness_interval_seconds=21600)
    assert p.freshness_interval_seconds == 21600


# ── Validation: enums ───────────────────────────────────────────────

def test_invalid_latency_class_rejected():
    with pytest.raises(ValueError, match="latency_class"):
        _minimal_profile(latency_class="ludicrous")


def test_invalid_volume_rejected():
    with pytest.raises(ValueError, match="expected_volume"):
        _minimal_profile(expected_volume="enormous")


def test_invalid_concurrency_mode_rejected():
    with pytest.raises(ValueError, match="concurrency.mode"):
        _minimal_profile(concurrency=ConcurrencyProfile(mode="random"))


def test_invalid_pagination_mode_rejected():
    with pytest.raises(ValueError, match="pagination.mode"):
        _minimal_profile(pagination=PaginationProfile(mode="psychic"))


def test_invalid_checkpoint_unit_rejected():
    with pytest.raises(ValueError, match="checkpoint.unit"):
        _minimal_profile(checkpoint=CheckpointProfile(unit="vibes"))


def test_invalid_auth_type_rejected():
    with pytest.raises(ValueError, match="auth.type"):
        _minimal_profile(auth=AuthProfile(type="handshake"))


def test_delta_checkpoint_requires_delta_field():
    """delta_token mode is meaningless without naming the field that
    holds the cursor — fail at registration not at runtime."""
    with pytest.raises(ValueError, match="delta_field"):
        _minimal_profile(checkpoint=CheckpointProfile(unit="delta_token"))


def test_delta_checkpoint_with_field_validates():
    p = _minimal_profile(checkpoint=CheckpointProfile(
        unit="delta_token", delta_field="next_token"))
    assert p.checkpoint.delta_field == "next_token"


# ── Profile is frozen ───────────────────────────────────────────────

def test_profile_is_immutable():
    """Profiles should be hashable + immutable — registries put them
    in dicts, and runtime mutation would be a footgun."""
    p = _minimal_profile()
    with pytest.raises(Exception):  # FrozenInstanceError on dataclass
        p.name = "different"  # type: ignore[misc]


# ── Realistic-shape profile (inline fixture, no vendor name) ────────

def test_complex_profile_with_all_optional_features_validates():
    """Locks down the canonical 'slow + fanout + nested' shape — every
    optional feature engaged at once. Uses inline fixture, not a
    vendor-named sample."""
    from fpulse.extraction.profile import EnrichmentProfile

    p = SourceProfile(
        name="slow_fanout_source",
        category="it_asset",
        notes="generic slow + fanout + nested shape",
        latency_class="very_slow",
        expected_volume="large",
        freshness_interval_seconds=6 * 3600,
        auth=AuthProfile(type="api_token", header="Authorization", prefix=""),
        pagination=PaginationProfile(
            mode="offset", items_path=["items"],
            page_size=100, offset_param="page", limit_param="page_size",
        ),
        enrichment=EnrichmentProfile(
            list_url="/api/list",
            list_id_field="id",
            fetch_url="/api/detail/{id}",
            batch_size=1,
        ),
        rate_limit=RateLimitProfile(rps=8.0, burst=12, respect_header="Retry-After"),
        concurrency=ConcurrencyProfile(mode="aimd", initial=4, max=12),
        schema=SchemaProfile(
            field_paths={
                "id": "id",
                "deeply_nested": "a.b.c.d",
                "wildcard_array": "items[*].name",
                "fallback": "missing.path|default=unknown",
                "timestamp": "scan_history[0].timestamp",
            },
            coercions={"timestamp": "iso_datetime"},
        ),
    )

    assert p.latency_class == "very_slow"
    assert p.freshness_interval_seconds == 6 * 3600
    assert p.enrichment is not None
    assert p.concurrency.mode == "aimd"
    assert p.rate_limit.respect_header == "Retry-After"
    # Schema includes deeply nested paths AND a wildcard AND a default.
    assert any("." in v and "[" not in v for v in p.schema.field_paths.values())
    assert any("[*]" in v for v in p.schema.field_paths.values())
    assert any("|default=" in v for v in p.schema.field_paths.values())
    assert "iso_datetime" in p.schema.coercions.values()
