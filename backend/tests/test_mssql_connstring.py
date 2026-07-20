"""Unit tests for the mssql bulk-loader connection-string builder
(2026-06-08). Pins Windows-Auth vs SQL-login selection — added so
on-prem SQL Server (which defaults to Windows/AD integrated auth) works
without a SQL login.
"""
from __future__ import annotations

from fpulse.engine.bulk_load.dialects.mssql import _build_connection_string


BASE = {"host": "db.example.com", "port": 1433, "database": "orders"}


def test_sql_login_uses_uid_pwd():
    cs = _build_connection_string({**BASE, "user": "app", "password": "secret"})
    assert "UID=app" in cs and "PWD=secret" in cs
    assert "Trusted_Connection" not in cs


def test_no_user_falls_back_to_windows_auth():
    cs = _build_connection_string(BASE)  # no user
    assert "Trusted_Connection=yes" in cs
    assert "UID=" not in cs and "PWD=" not in cs


def test_blank_user_is_windows_auth():
    cs = _build_connection_string({**BASE, "user": "  ", "password": ""})
    assert "Trusted_Connection=yes" in cs


def test_explicit_trusted_connection_flag_wins_over_user():
    cs = _build_connection_string({**BASE, "user": "app", "password": "x", "trusted_connection": True})
    assert "Trusted_Connection=yes" in cs
    assert "UID=" not in cs

def test_explicit_trusted_connection_false_forces_sql_login():
    cs = _build_connection_string({**BASE, "user": "app", "password": "x", "trusted_connection": "false"})
    assert "UID=app" in cs and "Trusted_Connection" not in cs


def test_driver_and_encrypt_defaults():
    cs = _build_connection_string(BASE)
    assert "ODBC Driver 18 for SQL Server" in cs
    assert "Encrypt=yes" in cs
    assert "TrustServerCertificate=no" in cs


def test_custom_driver_and_tls():
    cs = _build_connection_string({**BASE, "driver": "ODBC Driver 17 for SQL Server",
                                    "encrypt": "no", "trust_server_certificate": "yes"})
    assert "ODBC Driver 17 for SQL Server" in cs
    assert "Encrypt=no" in cs and "TrustServerCertificate=yes" in cs
