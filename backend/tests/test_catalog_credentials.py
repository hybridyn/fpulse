"""Tests for credential resolution — the precedence chain that lets a
catalog provider work the same in OSS (no Vault) and Plus (with Vault)
deployments."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from fpulse.connections.credentials import resolve_credentials


def _conn(**kwargs):
    """Build a minimal Connection-shaped object."""
    return SimpleNamespace(**{
        "config": {},
        "vault_key": None,
        "credential_id": None,
        **kwargs,
    })


def test_returns_empty_for_empty_connection():
    out = resolve_credentials(_conn())
    assert out == {}


def test_connection_config_is_baseline():
    out = resolve_credentials(_conn(config={"host": "localhost", "port": 5432}))
    assert out["host"] == "localhost"
    assert out["port"] == 5432


def test_override_config_wins_over_connection_config():
    """For /test-inline calls, the explicit override must take precedence."""
    out = resolve_credentials(
        _conn(config={"host": "old", "port": 5432}),
        override_config={"host": "new"},
    )
    assert out["host"] == "new"
    # Non-overridden keys still come through.
    assert out["port"] == 5432


def test_vault_unavailable_falls_back_to_config():
    """OSS deployment without the Vault stack must still produce
    a usable credential dict — no exceptions, no empty result."""
    # `_try_vault_get` returns None when app_state has no vault.
    with patch("fpulse.connections.credentials._try_vault_get", return_value=None):
        out = resolve_credentials(_conn(
            config={"host": "h", "password": "in-config"},
            vault_key="vault://abc",
        ))
    assert out["password"] == "in-config"


def test_vault_overrides_connection_config_when_present():
    """When Vault is bound, secrets in Vault should win over any
    duplicates in connection.config (the latter is for non-secret
    metadata only by convention)."""
    with patch(
        "fpulse.connections.credentials._try_vault_get",
        return_value={"password": "from-vault", "access_token": "abc"},
    ):
        out = resolve_credentials(_conn(
            config={"host": "h", "password": "from-config"},
            vault_key="vault://abc",
        ))
    assert out["password"] == "from-vault"
    assert out["access_token"] == "abc"
    assert out["host"] == "h"  # non-secret config preserved


def test_override_wins_over_vault():
    """Explicit /test-inline override beats Vault — useful for
    operators verifying a credential rotation before saving."""
    with patch(
        "fpulse.connections.credentials._try_vault_get",
        return_value={"password": "from-vault"},
    ):
        out = resolve_credentials(
            _conn(vault_key="vault://x"),
            override_config={"password": "from-test-form"},
        )
    assert out["password"] == "from-test-form"
