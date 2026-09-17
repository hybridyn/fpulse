"""Create and sign release checksum manifests.

The manifest format is the conventional ``sha256  filename`` text file,
sorted by filename for stable diffs. Signing uses Ed25519 so the release
pipeline does not depend on a local GPG keyring. The private key is supplied
through an environment variable as either:

  * base64 raw 32-byte Ed25519 private key, or
  * PEM text, optionally base64-encoded.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


DEFAULT_KEY_ENV = "FPULSE_RELEASE_ED25519_PRIVATE_KEY"
EXCLUDED_OUTPUTS = {"SHA256SUMS", "SHA256SUMS.sig", "SHA256SUMS.pub"}


def _candidate_files(directory: Path, patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for path in directory.iterdir():
        if not path.is_file() or path.name in EXCLUDED_OUTPUTS:
            continue
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns):
            files.append(path)
    return sorted(files, key=lambda item: item.name)


def write_checksums(directory: Path, patterns: list[str], output_name: str) -> Path:
    files = _candidate_files(directory, patterns)
    if not files:
        raise SystemExit(f"No release assets matched {patterns!r} in {directory}")

    lines = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")

    out = directory / output_name
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return out


def _load_private_key(value: str) -> Ed25519PrivateKey:
    raw_value = value.strip()
    payloads: list[bytes] = [raw_value.encode("utf-8")]
    try:
        payloads.append(base64.b64decode(raw_value, validate=True))
    except Exception:
        pass

    for payload in payloads:
        if payload.startswith(b"-----BEGIN"):
            key = serialization.load_pem_private_key(payload, password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise SystemExit("Signing key is not an Ed25519 private key")
            return key
        if len(payload) == 32:
            return Ed25519PrivateKey.from_private_bytes(payload)

    raise SystemExit(
        "Signing key must be PEM or base64 raw 32-byte Ed25519 private key"
    )


def sign_manifest(manifest: Path, key_value: str) -> tuple[Path, Path]:
    private_key = _load_private_key(key_value)
    signature = private_key.sign(manifest.read_bytes())
    public_key = private_key.public_key()

    sig_path = manifest.with_name(manifest.name + ".sig")
    pub_path = manifest.with_name(manifest.name + ".pub")
    sig_path.write_text(
        base64.b64encode(signature).decode("ascii") + "\n",
        encoding="ascii",
        newline="\n",
    )
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_path.write_text(
        base64.b64encode(pub_bytes).decode("ascii") + "\n",
        encoding="ascii",
        newline="\n",
    )
    return sig_path, pub_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create signed release checksums")
    parser.add_argument("--directory", default="dist", help="Directory containing release assets")
    parser.add_argument(
        "--pattern",
        action="append",
        default=None,
        help="Filename glob to include; repeatable (default: *)",
    )
    parser.add_argument("--output", default="SHA256SUMS", help="Checksum filename")
    parser.add_argument(
        "--key-env",
        default=DEFAULT_KEY_ENV,
        help=f"Environment variable containing the Ed25519 private key (default: {DEFAULT_KEY_ENV})",
    )
    parser.add_argument(
        "--require-signature",
        action="store_true",
        help="Fail if the signing key environment variable is absent",
    )
    args = parser.parse_args(argv)

    directory = Path(args.directory).resolve()
    manifest = write_checksums(directory, args.pattern or ["*"], args.output)
    print(f"wrote {manifest}")

    key_value = os.environ.get(args.key_env, "").strip()
    if key_value:
        sig_path, pub_path = sign_manifest(manifest, key_value)
        print(f"signed {manifest.name} -> {sig_path.name}, {pub_path.name}")
    elif args.require_signature:
        raise SystemExit(f"{args.key_env} is required to sign release checksums")
    else:
        print(f"warning: {args.key_env} not set; wrote unsigned checksums only")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
