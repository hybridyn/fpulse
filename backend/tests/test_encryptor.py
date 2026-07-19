"""Unit tests for the always-on Fernet encryptor (May 4 2026).

Replaces the previous Plus-gated path. These tests verify:
  * Round-trip encrypt → decrypt works for single values and config dicts
  * Sensitive fields are auto-detected by name
  * Legacy formats (`PLAIN:` prefix, raw plaintext) are tolerated on read
  * Master key file is created chmod 600 on first run
  * World-readable keys are rejected on POSIX
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from fpulse.security.encryptor import (
    Encryptor,
    load_or_create_master_key,
    master_key_path,
)


@pytest.fixture
def temp_master_key(tmp_path, monkeypatch):
    """Point FPULSE_MASTER_KEY_FILE at a temp file so tests don't touch ~/.fpulse/."""
    key_file = tmp_path / "secret.key"
    monkeypatch.setenv("FPULSE_MASTER_KEY_FILE", str(key_file))
    yield key_file


@pytest.fixture
def encryptor():
    """Encryptor backed by an ephemeral key — no filesystem touch."""
    return Encryptor(Fernet.generate_key())


# ── Single value round-trip ──────────────────────────────────────────


class TestValueRoundTrip:
    def test_basic(self, encryptor):
        ct = encryptor.encrypt_value("hunter2")
        assert ct.startswith("ENC:v1:")
        assert "hunter2" not in ct
        assert encryptor.decrypt_value(ct) == "hunter2"

    def test_unicode(self, encryptor):
        plain = "пароль 🔑 password"
        assert encryptor.decrypt_value(encryptor.encrypt_value(plain)) == plain

    def test_empty_passes_through(self, encryptor):
        assert encryptor.encrypt_value("") == ""
        assert encryptor.decrypt_value("") == ""

    def test_long_value(self, encryptor):
        plain = "A" * 10_000
        assert encryptor.decrypt_value(encryptor.encrypt_value(plain)) == plain


# ── Legacy format tolerance ──────────────────────────────────────────


class TestLegacyTolerance:
    def test_plain_prefix_decrypts_to_value(self, encryptor):
        # Old AI config store sentinel
        assert encryptor.decrypt_value("PLAIN:hunter2") == "hunter2"

    def test_raw_plaintext_returned_as_is(self, encryptor):
        # Very old OSS rows had no marker at all. Tolerated for migration
        # window — caller should re-save to upgrade to ENC:v1:.
        assert encryptor.decrypt_value("legacy_password") == "legacy_password"

    def test_corrupt_token_raises(self, encryptor):
        with pytest.raises(RuntimeError, match="Decryption failed"):
            encryptor.decrypt_value("ENC:v1:not_a_real_token")


# ── Config-dict API ──────────────────────────────────────────────────


class TestConfigRoundTrip:
    def test_sensitive_fields_encrypted(self, encryptor):
        config = {
            "host": "db.example.com",
            "port": 5432,
            "database": "prod",
            "user": "app",
            "password": "hunter2",
        }
        enc = encryptor.encrypt_config(config)
        # Plaintext fields untouched
        assert enc["host"] == "db.example.com"
        assert enc["port"] == 5432
        assert enc["user"] == "app"
        # Password ciphered
        assert enc["password"].startswith("ENC:v1:")
        assert "hunter2" not in enc["password"]
        # Round-trip
        dec = encryptor.decrypt_config(enc)
        assert dec == config

    def test_multiple_sensitive_keys(self, encryptor):
        config = {
            "api_key": "sk_live_abc",
            "client_secret": "shh",
            "passphrase": "p4ssphr4se",
            "host": "https://api.example.com",
        }
        enc = encryptor.encrypt_config(config)
        for k in ("api_key", "client_secret", "passphrase"):
            assert enc[k].startswith("ENC:v1:")
            assert config[k] not in enc[k]
        assert enc["host"] == config["host"]
        assert encryptor.decrypt_config(enc) == config

    def test_nested_config(self, encryptor):
        config = {
            "host": "h",
            "auth": {"token": "abc123", "scheme": "bearer"},
        }
        enc = encryptor.encrypt_config(config)
        assert enc["auth"]["token"].startswith("ENC:v1:")
        assert enc["auth"]["scheme"] == "bearer"
        assert encryptor.decrypt_config(enc) == config

    def test_empty_password_passes_through(self, encryptor):
        config = {"password": "", "host": "h"}
        enc = encryptor.encrypt_config(config)
        # Empty values are not encrypted (no ciphertext to produce).
        assert enc["password"] == ""

    def test_non_dict_passes_through(self, encryptor):
        # Defensive: legacy rows that happened to be lists or strings.
        assert encryptor.encrypt_config("oops") == "oops"
        assert encryptor.encrypt_config([1, 2, 3]) == [1, 2, 3]

    def test_decrypt_tolerates_mixed_row(self, encryptor):
        # During the migration window, rows can have ciphertext for some
        # fields and plaintext for others. A row encrypted with a DIFFERENT
        # master key (e.g. backup restored after key rotation) must
        # fail-loud rather than silently return garbage.
        other_key = Fernet.generate_key()
        bad_ciphertext = "ENC:v1:" + Fernet(other_key).encrypt(b"shh").decode()
        config = {
            "host": "h",
            "password": bad_ciphertext,                    # encrypted with wrong key
            "api_key": "still_plaintext_legacy",           # untouched legacy
        }
        with pytest.raises(RuntimeError, match="master key may have changed"):
            encryptor.decrypt_config(config)

    def test_decrypt_handles_legacy_plaintext_alongside_ciphertext(self, encryptor):
        # The complement of the above: a row where some fields are
        # already encrypted with OUR key and others are still raw
        # plaintext (very old OSS install). Must NOT raise — the legacy
        # plaintext field comes back as-is.
        config = {
            "host": "h",
            "password": encryptor.encrypt_value("hunter2"),  # properly encrypted
            "api_key": "PLAIN:legacy_apikey",                # AI-config sentinel
            "secret": "raw_legacy_secret",                   # very-old plaintext
        }
        out = encryptor.decrypt_config(config)
        assert out["host"] == "h"
        assert out["password"] == "hunter2"
        assert out["api_key"] == "legacy_apikey"
        assert out["secret"] == "raw_legacy_secret"


# ── Sensitive field detection ────────────────────────────────────────


class TestSensitiveDetection:
    @pytest.mark.parametrize("name", [
        "password", "secret", "key", "token", "private_key",
        "api_key", "apikey", "client_secret", "passphrase",
        "access_token", "refresh_token", "PASSWORD", "Api_Key",
    ])
    def test_recognized_fields(self, name):
        assert Encryptor._is_sensitive_field(name) is True

    @pytest.mark.parametrize("name", [
        "host", "port", "database", "user", "username",
        "schema", "table", "name", "id",
    ])
    def test_non_sensitive_fields(self, name):
        assert Encryptor._is_sensitive_field(name) is False


# ── Master key file management ───────────────────────────────────────


class TestMasterKeyPath:
    def test_explicit_env_var(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FPULSE_MASTER_KEY_FILE", str(tmp_path / "k"))
        assert master_key_path() == tmp_path / "k"

    def test_data_dir_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FPULSE_MASTER_KEY_FILE", raising=False)
        monkeypatch.setenv("FPULSE_DATA_DIR", str(tmp_path))
        assert master_key_path() == tmp_path / "secret.key"

    def test_home_fallback(self, monkeypatch):
        monkeypatch.delenv("FPULSE_MASTER_KEY_FILE", raising=False)
        monkeypatch.delenv("FPULSE_DATA_DIR", raising=False)
        assert master_key_path() == Path.home() / ".fpulse" / "secret.key"


class TestMasterKeyLifecycle:
    def test_first_run_creates_key(self, temp_master_key):
        assert not temp_master_key.exists()
        key = load_or_create_master_key()
        assert temp_master_key.exists()
        assert len(key) >= 32

    def test_subsequent_runs_read_existing(self, temp_master_key):
        first = load_or_create_master_key()
        second = load_or_create_master_key()
        assert first == second

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission check")
    def test_world_readable_key_rejected(self, temp_master_key):
        # Generate the key, then loosen permissions.
        load_or_create_master_key()
        os.chmod(temp_master_key, 0o644)
        with pytest.raises(RuntimeError, match="world readable"):
            load_or_create_master_key()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission check")
    def test_first_run_creates_chmod_600(self, temp_master_key):
        load_or_create_master_key()
        mode = temp_master_key.stat().st_mode
        # Owner read/write only.
        assert mode & 0o077 == 0


# ── Construction ─────────────────────────────────────────────────────


class TestConstruction:
    def test_from_master_key_loads_or_creates(self, temp_master_key):
        enc = Encryptor.from_master_key()
        assert isinstance(enc, Encryptor)
        # Round-trip works.
        assert enc.decrypt_value(enc.encrypt_value("ok")) == "ok"

    def test_invalid_key_raises(self):
        with pytest.raises(RuntimeError, match="Invalid Fernet key"):
            Encryptor(b"too short")
