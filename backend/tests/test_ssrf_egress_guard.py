"""SSRF egress guards on the probe / management endpoints.

Ported from the monorepo 2026-07-16 — the shared core had this fix; the OSS
export did not, so `security/ssrf.py` shipped without `is_internal_host` and
the three egress paths (AI-provider probe, Ollama resolve, connection base_url)
fetched caller-supplied URLs with no internal-host check. A non-admin could aim
the AI-provider probe at http://169.254.169.254/ (cloud metadata) and read the
response — the probe POSTs the api_key to base_url and returns the result.

The guard is admin-aware on purpose: loopback (local Ollama) is allowed for
everyone; any other private/metadata/reserved target is admin-only.
"""
from __future__ import annotations

import pytest

from fpulse.security.ssrf import is_internal_host


class _User:
    def __init__(self, role):
        self.role = role


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://169.254.169.254/latest/meta-data/", True),   # AWS/GCP/Azure metadata
        ("http://[fd00::1]/", True),                           # private IPv6
        ("http://10.0.0.5/admin", True),                       # RFC1918
        ("http://192.168.1.1/", True),
        ("http://127.0.0.1:11434", False),                     # loopback — local Ollama
        ("http://localhost:8080", False),                      # loopback name
        ("https://api.openai.com", False),                     # public
        ("", False),                                           # empty
        ("not a url", False),                                  # malformed -> fail-open
    ],
)
def test_is_internal_host_classification(url, expected):
    assert is_internal_host(url) is expected


def test_probe_guard_blocks_non_admin_internal():
    from fpulse.api.ai_config import _probe_ssrf_guard
    from fastapi import HTTPException

    # Non-admin aiming the probe at cloud metadata -> 403.
    with pytest.raises(HTTPException) as ei:
        _probe_ssrf_guard("http://169.254.169.254/", _User("developer"))
    assert ei.value.status_code == 403


def test_probe_guard_allows_admin_internal():
    from fpulse.api.ai_config import _probe_ssrf_guard
    # Admins may legitimately probe an on-box / internal model host.
    _probe_ssrf_guard("http://10.0.0.5:11434", _User("admin"))
    _probe_ssrf_guard("http://169.254.169.254/", _User("super_admin"))


def test_probe_guard_allows_everyone_loopback_and_public():
    from fpulse.api.ai_config import _probe_ssrf_guard
    # Loopback (local Ollama) and public endpoints are fine for any role.
    _probe_ssrf_guard("http://127.0.0.1:11434", _User("developer"))
    _probe_ssrf_guard("https://api.openai.com", _User("developer"))
    _probe_ssrf_guard("", _User("developer"))
