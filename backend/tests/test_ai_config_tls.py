"""TLS handling for AI provider probes (fpulse.api.ai_config).

Covers the fix for `[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in
certificate chain` — the error users behind a TLS-intercepting proxy/AV
(corporate MITM, Kaspersky, Zscaler) hit when testing a cloud provider.
"""

from __future__ import annotations

from fpulse.api import ai_config
import ssl
import certifi
import pytest
from pathlib import Path

_ALL = ("FPULSE_AI_INSECURE_TLS", "FPULSE_AI_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE")


def _clear(monkeypatch):
    for v in _ALL:
        monkeypatch.delenv(v, raising=False)


def test_default_verify_is_true(monkeypatch):
    _clear(monkeypatch)
    context = ai_config._ai_verify()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_legacy_insecure_flag_does_not_disable_verify(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("FPULSE_AI_INSECURE_TLS", "1")
    assert ai_config._ai_verify().verify_mode == ssl.CERT_REQUIRED


def test_ca_bundle_path_is_used_when_it_exists(tmp_path, monkeypatch):
    _clear(monkeypatch)
    ca = tmp_path / "corp-root.pem"
    ca.write_bytes(Path(certifi.where()).read_bytes())
    monkeypatch.setenv("FPULSE_AI_CA_BUNDLE", str(ca))
    assert ai_config._ai_verify().cert_store_stats()['x509_ca'] > 0


def test_missing_ca_path_fails_closed(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("FPULSE_AI_CA_BUNDLE", str(tmp_path / "does-not-exist.pem"))
    with pytest.raises(FileNotFoundError):
        ai_config._ai_verify()


def test_invalid_bundle_fails_even_with_legacy_insecure_flag(tmp_path, monkeypatch):
    _clear(monkeypatch)
    ca = tmp_path / "c.pem"
    ca.write_text("x", encoding="utf-8")
    monkeypatch.setenv("FPULSE_AI_CA_BUNDLE", str(ca))
    monkeypatch.setenv("FPULSE_AI_INSECURE_TLS", "yes")
    with pytest.raises(ssl.SSLError):
        ai_config._ai_verify()


def test_friendly_error_detects_tls_interception():
    exc = Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                    "self-signed certificate in certificate chain")
    msg = ai_config._friendly_probe_error(exc)
    assert "TLS certificate verification failed" in msg
    assert "FPULSE_AI_CA_BUNDLE" in msg
    assert "FPULSE_AI_INSECURE_TLS" not in msg


def test_friendly_error_passes_other_errors_through():
    assert ai_config._friendly_probe_error(Exception("401 Unauthorized")) == "probe error: 401 Unauthorized"
