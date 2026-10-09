"""Tests for signed connector 'Verified' attestations (Ed25519).

Proves the Plus differentiator end to end: a green CAT run can be signed into
an offline-verifiable attestation, tampering is detected, an untrusted issuer
is rejected, and signing is gated on a key + a green suite.
"""

from __future__ import annotations

import json

import pytest

from fpulse.connectors import attest
from fpulse.connectors.cat import CatReport, CheckResult


def _green_report(connector: str = "demo") -> CatReport:
    return CatReport(
        connector=connector, mode="replay", run_at="2026-10-08T00:00:00+00:00",
        manifest_sha256="a" * 64,
        checks=[
            CheckResult("-", "spec", "pass"),
            CheckResult("things", "run_happy_path", "pass"),
            CheckResult("things", "schema_conformance", "pass"),
            CheckResult("things", "empty", "pass"),
        ],
        fixture_sha256={"things.happy_path.cassette.json": "b" * 64},
    )


def test_sign_and_verify_roundtrip():
    priv, pub = attest.generate_keypair()
    env = attest.attest_connector("demo", private_key_source=priv, report=_green_report())
    ok, reason = attest.verify_attestation(env, trusted_public_key=pub)
    assert ok, reason
    assert "pinned key" in reason
    assert env["signature"]["alg"] == "ed25519"
    assert env["attestation"]["cat_level"] == "verified"
    assert env["attestation"]["fixture_sha256"]  # fixtures are pinned into the claim


def test_verify_without_pin_reports_key_id():
    priv, _pub = attest.generate_keypair()
    env = attest.attest_connector("demo", private_key_source=priv, report=_green_report())
    ok, reason = attest.verify_attestation(env)  # no pin
    assert ok
    assert env["signature"]["key_id"] in reason


def test_tamper_is_detected():
    priv, pub = attest.generate_keypair()
    env = attest.attest_connector("demo", private_key_source=priv, report=_green_report())
    # Flip a byte of the signed claim — the signature must no longer verify.
    env["attestation"]["manifest_sha256"] = "c" * 64
    ok, reason = attest.verify_attestation(env, trusted_public_key=pub)
    assert not ok
    assert "INVALID" in reason


def test_untrusted_key_is_rejected_even_if_math_valid():
    priv_a, _pub_a = attest.generate_keypair()
    _priv_b, pub_b = attest.generate_keypair()
    env = attest.attest_connector("demo", private_key_source=priv_a, report=_green_report())
    ok, reason = attest.verify_attestation(env, trusted_public_key=pub_b)
    assert not ok
    assert "UNTRUSTED" in reason


def test_signing_requires_a_key(monkeypatch):
    monkeypatch.delenv("FPULSE_ATTEST_PRIVATE_KEY", raising=False)
    with pytest.raises(attest.AttestationError) as ei:
        attest.attest_connector("demo", report=_green_report())
    assert "F-Pulse+" in str(ei.value)


def test_refuses_to_sign_a_red_cat():
    priv, _pub = attest.generate_keypair()
    red = _green_report()
    red.checks.append(CheckResult("things", "run_happy_path", "fail", "boom"))
    with pytest.raises(attest.AttestationError) as ei:
        attest.attest_connector("demo", private_key_source=priv, report=red)
    assert "not green" in str(ei.value)


def test_write_and_verify_connector_file(tmp_path):
    priv, pub = attest.generate_keypair()
    env = attest.attest_connector("demo", private_key_source=priv, report=_green_report())
    p = tmp_path / "demo.json"
    attest.write_attestation(env, p, connector_id="demo")
    assert p.is_file()
    ok, reason, loaded = attest.verify_connector("demo", path=p, trusted_public_key=pub)
    assert ok, reason
    assert loaded["attestation"]["connector"] == "demo"


def test_attest_github_end_to_end():
    """Sign the REAL github connector (green via its committed CAT fixtures),
    then verify — the full cat -> attest -> verify chain."""
    from fpulse.connectors.cat import discover_certifiable
    if "github" not in discover_certifiable():
        pytest.skip("github CAT fixtures not present")
    priv, pub = attest.generate_keypair()
    env = attest.attest_connector("github", private_key_source=priv)
    assert env["attestation"]["connector"] == "github"
    assert env["attestation"]["cat_level"] == "verified"
    ok, reason = attest.verify_attestation(env, trusted_public_key=pub)
    assert ok, reason
