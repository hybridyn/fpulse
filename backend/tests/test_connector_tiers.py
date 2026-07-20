"""Connector confidence tiers.

A connector that ships a `<id>.v2.json` cert manifest is auto-CERTIFIED; the
rest default to BETA. The picker options carry the tier (and suffix it into the
label for non-certified) so a beta connector never looks equal to a certified
one. An explicit `tier` in the JSON wins.
"""

from __future__ import annotations

from fpulse.connectors.rest_framework import (
    RestConnectorManifest,
    _connector_options,
    get_manifest,
    load_manifests,
)


def test_v2_backed_connector_is_certified():
    load_manifests(force=True)
    # salesforce ships salesforce.v2.json → certified
    sf = get_manifest("salesforce")
    assert sf is not None and sf.tier == "certified"


def test_connector_without_cert_is_beta():
    load_manifests(force=True)
    # zendesk has no .v2.json → beta
    zd = get_manifest("zendesk")
    assert zd is not None and zd.tier == "beta"


def test_explicit_tier_in_json_wins():
    m = RestConnectorManifest.from_dict({"id": "x", "name": "X", "tier": "community"})
    assert m.tier == "community"


def test_default_tier_is_beta():
    m = RestConnectorManifest.from_dict({"id": "x", "name": "X"})
    assert m.tier == "beta"


def test_picker_options_carry_tier_and_suffix_noncertified():
    opts = _connector_options()
    by_value = {o["value"]: o for o in opts}
    assert by_value["salesforce"]["tier"] == "certified"
    assert by_value["salesforce"]["label"] == "Salesforce"  # certified = clean label
    assert by_value["zendesk"]["tier"] == "beta"
    assert "beta" in by_value["zendesk"]["label"].lower()    # tier surfaced in label
