import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from scripts.release_checksums import main, write_checksums


def test_write_checksums_is_sorted_and_excludes_outputs(tmp_path):
    (tmp_path / "b.AppImage").write_text("b", encoding="utf-8")
    (tmp_path / "a.deb").write_text("a", encoding="utf-8")
    (tmp_path / "SHA256SUMS").write_text("old", encoding="utf-8")

    manifest = write_checksums(tmp_path, ["*.deb", "*.AppImage"], "SHA256SUMS")

    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("  a.deb")
    assert lines[1].endswith("  b.AppImage")
    assert len(lines) == 2


def test_release_checksums_signs_with_ed25519_env(monkeypatch, tmp_path):
    (tmp_path / "fpulse.deb").write_text("deb", encoding="utf-8")
    private_key = Ed25519PrivateKey.generate()
    raw_private = private_key.private_bytes_raw()
    monkeypatch.setenv(
        "FPULSE_RELEASE_ED25519_PRIVATE_KEY",
        base64.b64encode(raw_private).decode("ascii"),
    )

    rc = main(["--directory", str(tmp_path), "--pattern", "*.deb", "--require-signature"])

    assert rc == 0
    manifest = tmp_path / "SHA256SUMS"
    sig = base64.b64decode((tmp_path / "SHA256SUMS.sig").read_text(encoding="ascii"))
    pub = base64.b64decode((tmp_path / "SHA256SUMS.pub").read_text(encoding="ascii"))
    Ed25519PublicKey.from_public_bytes(pub).verify(sig, manifest.read_bytes())


def test_release_checksums_require_signature_fails_without_key(monkeypatch, tmp_path):
    (tmp_path / "fpulse.deb").write_text("deb", encoding="utf-8")
    monkeypatch.delenv("FPULSE_RELEASE_ED25519_PRIVATE_KEY", raising=False)

    with pytest.raises(SystemExit, match="required to sign release checksums"):
        main(["--directory", str(tmp_path), "--pattern", "*.deb", "--require-signature"])
