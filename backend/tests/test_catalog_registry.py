"""Tests for the catalog registry — the auditable surface.

These guard the public counts that release notes / docs cite: anything
that drifts here is a documentation bug. The tests are intentionally
simple — they're the safety net for the "the numbers must match"
contract we promised in three review cycles.
"""

from __future__ import annotations

import pytest

from fpulse.connections.catalog import (
    Catalog,
    CatalogItem,
    ProviderMeta,
    get_catalog,
    registry_status,
    _PROVIDERS,
    _PROVIDER_META,
    _REAL_TYPES,
    _NO_CATALOG_TYPES,
    _PLANNED_TYPES,
)
from fpulse.connections.models import CONNECTION_TYPES


# ────────────────────────────────────────────────────────────────────
#  Registry shape
# ────────────────────────────────────────────────────────────────────

def test_registry_sets_are_disjoint():
    """A connector type must be in exactly ONE of real/no_catalog/planned.
    Overlap is a registration bug — would double-count."""
    real = set(_REAL_TYPES)
    nc = set(_NO_CATALOG_TYPES)
    planned = set(_PLANNED_TYPES)
    assert real & nc == set(), f"Overlap real ∩ no_catalog: {real & nc}"
    assert real & planned == set(), f"Overlap real ∩ planned: {real & planned}"
    assert nc & planned == set(), f"Overlap no_catalog ∩ planned: {nc & planned}"


def test_registry_status_counts_match_set_sizes():
    status = registry_status()
    assert status["counts"]["real"] == len(_REAL_TYPES)
    assert status["counts"]["no_catalog"] == len(_NO_CATALOG_TYPES)
    assert status["counts"]["planned"] == len(_PLANNED_TYPES)
    assert status["counts"]["total_registered"] == len(_PROVIDERS)


def test_total_registered_equals_sum_of_categories():
    s = registry_status()["counts"]
    assert s["real"] + s["no_catalog"] + s["planned"] == s["total_registered"]


def test_every_provider_has_metadata():
    """Each registered type must have ProviderMeta — otherwise the
    response can't carry category / auth / tier."""
    missing = [t for t in _PROVIDERS if t not in _PROVIDER_META]
    assert missing == [], f"Providers without meta: {missing}"


def test_every_known_connection_type_is_registered():
    """Every type listed in CONNECTION_TYPES must appear in the
    catalog registry — either as real, no_catalog, or planned. An
    unregistered type silently returns the generic 'not implemented'
    fallback and skews the docs counts."""
    registered = set(_PROVIDERS)
    missing = [t for t in CONNECTION_TYPES if t not in registered]
    assert missing == [], f"CONNECTION_TYPES without registry entry: {missing}"


# ────────────────────────────────────────────────────────────────────
#  Provider contracts (cost / compute)
# ────────────────────────────────────────────────────────────────────

def test_warehouse_providers_declare_no_billed_metadata():
    """Per architecture decision: warehouse catalog browse must use
    free metadata paths. If anything declares billed_metadata=True, a
    future user's bill becomes a surprise."""
    offenders = [
        t for t, meta in _PROVIDER_META.items()
        if meta.category == "warehouse" and meta.billed_metadata
    ]
    assert offenders == [], f"Warehouse providers with billed_metadata=True: {offenders}"


def test_warehouse_providers_declare_no_compute_requirement():
    """Catalog browse must never spin up a warehouse / cluster."""
    offenders = [
        t for t, meta in _PROVIDER_META.items()
        if meta.category == "warehouse" and meta.requires_compute
    ]
    assert offenders == [], f"Warehouse providers with requires_compute=True: {offenders}"


# ────────────────────────────────────────────────────────────────────
#  Response stamping
# ────────────────────────────────────────────────────────────────────

def test_get_catalog_for_unknown_type_returns_unsupported():
    cat = get_catalog("not_a_real_connector", {})
    assert cat.supported is False
    assert "not yet implemented" in cat.reason.lower() or "planned" in cat.reason.lower()


def test_get_catalog_stamps_meta_on_planned_response():
    """A planned connector must still report its category/tier so the
    UI can group it correctly even before the impl lands."""
    # Pick any planned type — salesforce is OAuth2 SaaS in the registry.
    cat = get_catalog("salesforce", {})
    assert cat.supported is False
    assert cat.category == "saas"
    assert cat.tier == "tier1"


def test_get_catalog_stamps_meta_on_no_catalog_response():
    """Write-only connectors stamp category=notification too."""
    cat = get_catalog("slack", {})
    assert cat.supported is False
    assert cat.category == "notification"


# ────────────────────────────────────────────────────────────────────
#  Build helper sanity
# ────────────────────────────────────────────────────────────────────

def test_build_derives_distinct_parents_and_kinds():
    from fpulse.connections.catalog import _build
    items = [
        CatalogItem(name="a", kind="table", parent="dbo"),
        CatalogItem(name="b", kind="table", parent="dbo"),
        CatalogItem(name="c", kind="view", parent="sales"),
    ]
    cat = _build(items)
    assert sorted(cat.parents) == ["dbo", "sales"]
    assert sorted(cat.kinds) == ["table", "view"]
