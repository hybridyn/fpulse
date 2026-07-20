"""Tests for the 2026-06-02 tier-system addition to cert_matrix.

Covers the new `tier` field computation + `?include_hidden` filter +
back-compat for the pre-existing `"hidden": true` boolean flag on
the three ads manifests.

These tests pin the contract so a future refactor of `_compute_tier`
or `cert_matrix()` can't quietly demote / promote / un-hide the
catalog.
"""
from __future__ import annotations

from fpulse.api.cert_matrix import (
    _compute_tier,
    _TIER_ORDER,
    _VALID_TIERS,
    cert_matrix,
)


# ── _compute_tier — the rules table ──────────────────────────────────────


def test_v1_capability_3_lands_in_beta():
    """A working v1 manifest (auth + streams + pagination) = Beta."""
    row = {
        "depth_score": 0,
        "validation_status": "uncertified",
        "issues_count": 0,
        "v1_capability_score": 3,
        "manifest_version": 1,
    }
    assert _compute_tier(row, {}) == "beta"


def test_v1_capability_under_3_lands_in_experimental():
    row = {
        "depth_score": 0,
        "validation_status": "uncertified",
        "issues_count": 0,
        "v1_capability_score": 2,
        "manifest_version": 1,
    }
    assert _compute_tier(row, {}) == "experimental"


def test_v2_pass_with_depth_1_is_beta():
    row = {
        "depth_score": 1,
        "validation_status": "pass",
        "issues_count": 0,
        "manifest_version": 2,
    }
    assert _compute_tier(row, {}) == "beta"


def test_v2_depth_3_without_fixture_stays_beta():
    """depth >= 3 isn't enough — Verified also requires a smoke fixture."""
    row = {
        "depth_score": 3,
        "validation_status": "pass",
        "issues_count": 0,
        "manifest_version": 2,
    }
    assert _compute_tier(row, {}, has_smoke_fixture=False) == "beta"


def test_v2_depth_3_with_fixture_promotes_to_verified():
    row = {
        "depth_score": 3,
        "validation_status": "pass",
        "issues_count": 0,
        "manifest_version": 2,
    }
    assert _compute_tier(row, {}, has_smoke_fixture=True) == "verified"


def test_production_requires_both_fixture_and_live_smoke_allowlist():
    row = {
        "depth_score": 5,
        "validation_status": "pass",
        "issues_count": 0,
        "manifest_version": 2,
    }
    # Fixture without allow-list → Verified, not Production
    assert _compute_tier(row, {}, has_smoke_fixture=True,
                         in_live_smoke_allowlist=False) == "verified"
    # Allow-list without fixture → also not Production
    assert _compute_tier(row, {}, has_smoke_fixture=False,
                         in_live_smoke_allowlist=True) == "beta"
    # Both → Production
    assert _compute_tier(row, {}, has_smoke_fixture=True,
                         in_live_smoke_allowlist=True) == "production"


# ── Declared tier — opt-DOWN allowed, opt-UP rejected ────────────────────


def test_manifest_can_opt_down_from_beta_to_experimental():
    row = {
        "depth_score": 0,
        "validation_status": "uncertified",
        "issues_count": 0,
        "v1_capability_score": 3,  # would compute to beta
        "manifest_version": 1,
    }
    assert _compute_tier(row, {"tier": "experimental"}) == "experimental"


def test_manifest_cannot_opt_up_to_verified():
    """Author declaring `tier: verified` on a stub manifest is ignored;
    computed beta wins."""
    row = {
        "depth_score": 0,
        "validation_status": "uncertified",
        "issues_count": 0,
        "v1_capability_score": 3,
        "manifest_version": 1,
    }
    # Declared verified is HIGHER than computed beta → ignored
    assert _compute_tier(row, {"tier": "verified"}) == "beta"


def test_invalid_tier_value_is_ignored():
    """A garbage `tier` value falls through to the computed tier."""
    row = {
        "depth_score": 0,
        "validation_status": "uncertified",
        "issues_count": 0,
        "v1_capability_score": 3,
        "manifest_version": 1,
    }
    assert _compute_tier(row, {"tier": "obvious_garbage"}) == "beta"


# ── Hidden — declared, boolean back-compat, visibility field ─────────────


def test_declared_tier_hidden_is_honoured():
    row = {
        "depth_score": 5,
        "validation_status": "pass",
        "issues_count": 0,
        "manifest_version": 2,
    }
    assert _compute_tier(row, {"tier": "hidden"},
                         has_smoke_fixture=True,
                         in_live_smoke_allowlist=True) == "hidden"


def test_back_compat_boolean_hidden_flag():
    """Pre-existing manifests with the old `hidden: true` boolean flag
    (google_ads, linkedin_ads, facebook_ads) still get Hidden tier."""
    row = {
        "depth_score": 0,
        "validation_status": "uncertified",
        "issues_count": 0,
        "v1_capability_score": 3,
        "manifest_version": 1,
    }
    assert _compute_tier(row, {"hidden": True}) == "hidden"


def test_visibility_field_forces_hidden():
    row = {
        "depth_score": 0,
        "validation_status": "uncertified",
        "issues_count": 0,
        "v1_capability_score": 3,
        "manifest_version": 1,
    }
    assert _compute_tier(row, {"visibility": "hidden"}) == "hidden"


# ── Endpoint-level: by_tier aggregate + ?include_hidden filter ───────────


def test_cert_matrix_returns_by_tier_aggregate():
    """The new by_tier field appears alongside the legacy by_label."""
    r = cert_matrix(include_hidden=True)
    assert "by_tier" in r
    assert "by_label" in r  # back-compat preserved
    # Every tier value in rows must be in the valid set
    for row in r["rows"]:
        assert row["tier"] in _VALID_TIERS


def test_cert_matrix_filters_hidden_by_default():
    """Default response excludes Hidden manifests."""
    visible = cert_matrix(include_hidden=False)
    hidden_ids_in_visible = [r["id"] for r in visible["rows"] if r["tier"] == "hidden"]
    assert hidden_ids_in_visible == [], (
        f"Hidden rows leaked into default listing: {hidden_ids_in_visible}"
    )


def test_cert_matrix_hidden_total_matches_actual_hidden():
    """The reported hidden_total counts ALL hidden rows even when filtered out."""
    r_default = cert_matrix(include_hidden=False)
    r_all = cert_matrix(include_hidden=True)
    actual_hidden = sum(1 for row in r_all["rows"] if row["tier"] == "hidden")
    assert r_default["hidden_total"] == actual_hidden
    assert r_default["total"] + actual_hidden == r_all["total"]


def test_tier_order_is_monotone():
    """Verifies the rank used by the opt-down rule has no gaps."""
    expected = ["hidden", "experimental", "beta", "verified", "production"]
    actual_sorted = sorted(_TIER_ORDER.items(), key=lambda x: x[1])
    assert [t for t, _ in actual_sorted] == expected
