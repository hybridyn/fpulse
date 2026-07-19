"""Unit tests for CredentialStore."""

import pytest
from fpulse.credentials.models import Credential
from fpulse.credentials.store import CredentialStore


class TestCredentialStore:
    def test_create(self, credential_store, sample_credential):
        c = credential_store.create(sample_credential)
        assert c.id == "cred-001"
        assert c.name == "Test DB"
        assert credential_store.count() == 1

    def test_get(self, credential_store, sample_credential):
        credential_store.create(sample_credential)
        c = credential_store.get("cred-001")
        assert c is not None
        assert c.type == "postgresql"

    def test_get_nonexistent(self, credential_store):
        assert credential_store.get("nope") is None

    def test_list_all_masks_secrets(self, credential_store, sample_credential):
        credential_store.create(sample_credential)
        result = credential_store.list_all()
        assert len(result) == 1
        # Password should be masked
        assert result[0]["config"]["password"] == "se***"
        # Non-sensitive fields should be plain
        assert result[0]["config"]["host"] == "localhost"

    def test_list_by_type(self, credential_store, sample_credential):
        credential_store.create(sample_credential)
        c2 = Credential(id="cred-002", name="API Key", type="rest_api",
                         config={"api_key": "xyz123"})
        credential_store.create(c2)
        result = credential_store.list_all(cred_type="postgresql")
        assert len(result) == 1
        assert result[0]["type"] == "postgresql"

    def test_list_by_project(self, credential_store):
        credential_store.create(Credential(id="c1", name="A", type="pg", project_id="p1"))
        credential_store.create(Credential(id="c2", name="B", type="pg", project_id="p2"))
        result = credential_store.list_all(project_id="p1")
        assert len(result) == 1

    def test_get_raw_returns_unmasked(self, credential_store, sample_credential):
        credential_store.create(sample_credential)
        raw = credential_store.get_raw("cred-001")
        assert raw is not None
        assert raw.config["password"] == "secret123"

    def test_update(self, credential_store, sample_credential):
        credential_store.create(sample_credential)
        updated = credential_store.update("cred-001", {"name": "Production DB"})
        assert updated is not None
        assert updated.name == "Production DB"

    def test_update_nonexistent(self, credential_store):
        assert credential_store.update("nope", {"name": "X"}) is None

    def test_delete(self, credential_store, sample_credential):
        credential_store.create(sample_credential)
        assert credential_store.delete("cred-001") is True
        assert credential_store.get("cred-001") is None

    def test_delete_nonexistent(self, credential_store):
        assert credential_store.delete("nope") is False

    def test_mark_used(self, credential_store, sample_credential):
        credential_store.create(sample_credential)
        credential_store.mark_used("cred-001")
        c = credential_store.get("cred-001")
        assert c.last_used is not None


class TestConfigMasking:
    def test_masks_password(self):
        result = CredentialStore._mask_config({"password": "longpassword"})
        assert result["password"] == "lo***"

    def test_masks_secret(self):
        result = CredentialStore._mask_config({"secret": "mysecret"})
        assert result["secret"] == "my***"

    def test_masks_api_key(self):
        result = CredentialStore._mask_config({"api_key": "key123"})
        assert result["api_key"] == "ke***"

    def test_masks_token(self):
        result = CredentialStore._mask_config({"token": "tok456"})
        assert result["token"] == "to***"

    def test_short_secret_fully_masked(self):
        result = CredentialStore._mask_config({"password": "ab"})
        assert result["password"] == "***"

    def test_non_sensitive_unchanged(self):
        result = CredentialStore._mask_config({"host": "localhost", "port": 5432})
        assert result["host"] == "localhost"
        assert result["port"] == 5432
