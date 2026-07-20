"""Unit tests for UserStore and auth models."""

import pytest
from fpulse.auth.models import User
from fpulse.auth.store import UserStore


class TestUserModel:
    def test_hash_password(self):
        # 2026-06-03 (H1) — assertions updated for the bcrypt migration.
        # Previous: `assert ":" in hashed` matched the legacy
        # `<salt>:<sha256>` format. Bcrypt hashes are `$2b$<cost>$<salt+hash>`
        # — no colon, ~60 chars.
        hashed = User.hash_password("secret123")
        assert hashed.startswith("$2"), (
            f"Expected bcrypt hash starting with $2a/$2b/$2y, got: {hashed[:8]}"
        )
        assert 55 <= len(hashed) <= 72, (
            f"bcrypt hashes are ~60 chars, got: {len(hashed)}"
        )

    def test_verify_password_correct(self):
        user = User(email="test@test.com", password_hash=User.hash_password("mypass"))
        assert user.verify_password("mypass") is True

    def test_verify_password_wrong(self):
        user = User(email="test@test.com", password_hash=User.hash_password("mypass"))
        assert user.verify_password("wrongpass") is False

    def test_verify_password_empty_hash(self):
        user = User(email="test@test.com", password_hash="")
        assert user.verify_password("anything") is False

    def test_hash_is_salted(self):
        """Two hashes of the same password should differ (salted)."""
        h1 = User.hash_password("same")
        h2 = User.hash_password("same")
        assert h1 != h2

    # 2026-06-03 (H1) — coverage for the legacy-hash backward-compat path.
    # Existing installs created before the bcrypt migration have
    # `<salt>:<sha256>` password hashes. verify_password must accept them
    # so users don't need a password reset; needs_rehash() must flag them
    # so the login flow upgrades to bcrypt in place on the next login.
    def test_legacy_sha256_hash_still_verifies(self):
        import hashlib
        import secrets as _secrets
        salt = _secrets.token_hex(16)
        legacy = f"{salt}:{hashlib.sha256(f'{salt}:hunter2'.encode()).hexdigest()}"
        u = User(email="legacy@t", password_hash=legacy)
        assert u.verify_password("hunter2") is True
        assert u.verify_password("wrong") is False
        assert u.needs_rehash() is True

    def test_fresh_bcrypt_hash_does_not_need_rehash(self):
        u = User(email="fresh@t", password_hash=User.hash_password("hunter2"))
        assert u.needs_rehash() is False


class TestUserStore:
    def test_default_admin_exists(self, user_store):
        admin = user_store.get_user("admin")
        assert admin is not None
        assert admin.email == "admin@fpulse.local"
        assert admin.role == "admin"

    def test_default_admin_password(self, user_store):
        # The bootstrap password is now random (secrets.token_urlsafe(18)),
        # written once to INITIAL_ADMIN_PASSWORD.txt. We can't assume a
        # static value — verify the hashing round-trip works by rotating
        # to a known value.
        admin = user_store.get_user("admin")
        admin.password_hash = User.hash_password("Rotated!Pass2026")
        user_store._save_user(admin)
        fresh = user_store.get_user("admin")
        assert fresh.verify_password("Rotated!Pass2026") is True
        assert fresh.verify_password("admin") is False

    def test_create_user(self, user_store):
        user = User(id="u1", email="user@test.com", name="Test User",
                     password_hash=User.hash_password("pass123"))
        created = user_store.create_user(user)
        assert created.id == "u1"

    def test_get_user_by_email(self, user_store):
        user = User(id="u1", email="user@test.com")
        user_store.create_user(user)
        found = user_store.get_user_by_email("user@test.com")
        assert found is not None
        assert found.id == "u1"

    def test_get_user_by_email_nonexistent(self, user_store):
        assert user_store.get_user_by_email("nope@nope.com") is None

    def test_list_users(self, user_store):
        result = user_store.list_users()
        assert len(result) == 1  # admin
        assert result[0]["email"] == "admin@fpulse.local"

    def test_update_user(self, user_store):
        updated = user_store.update_user("admin", {"name": "Super Admin"})
        assert updated.name == "Super Admin"

    def test_update_user_blocks_password_hash(self, user_store):
        """Should not allow direct password_hash update."""
        original_hash = user_store.get_user("admin").password_hash
        user_store.update_user("admin", {"password_hash": "hacked"})
        assert user_store.get_user("admin").password_hash == original_hash

    def test_update_nonexistent(self, user_store):
        assert user_store.update_user("nope", {"name": "X"}) is None

    def test_delete_user(self, user_store):
        user = User(id="u1", email="del@test.com")
        user_store.create_user(user)
        assert user_store.delete_user("u1") is True
        assert user_store.get_user("u1") is None

    def test_cannot_delete_admin(self, user_store):
        assert user_store.delete_user("admin") is False
        assert user_store.get_user("admin") is not None

    def test_delete_nonexistent(self, user_store):
        assert user_store.delete_user("nope") is False


class TestSessions:
    def test_create_session(self, user_store):
        session = user_store.create_session("admin")
        assert session.token
        assert session.user_id == "admin"

    def test_get_session(self, user_store):
        session = user_store.create_session("admin")
        found = user_store.get_session(session.token)
        assert found is not None
        assert found.user_id == "admin"

    def test_get_session_nonexistent(self, user_store):
        assert user_store.get_session("bad-token") is None

    def test_delete_session(self, user_store):
        session = user_store.create_session("admin")
        assert user_store.delete_session(session.token) is True
        assert user_store.get_session(session.token) is None

    def test_get_user_for_session(self, user_store):
        session = user_store.create_session("admin")
        user = user_store.get_user_for_session(session.token)
        assert user is not None
        assert user.id == "admin"

    def test_get_user_for_invalid_session(self, user_store):
        assert user_store.get_user_for_session("bad") is None

    def test_session_updates_last_login(self, user_store):
        user_store.create_session("admin")
        admin = user_store.get_user("admin")
        assert admin.last_login_at is not None
