"""Signed connector "Verified" attestations (Ed25519).

CAT (`fpulse.connectors.cat`) produces the *evidence* that a connector passed
its acceptance suite. This module makes that evidence **provable**: it signs a
CatReport into an offline-verifiable Ed25519 attestation, so a "Verified" badge
is not a label in someone's database but a cryptographic claim anyone can
check — pinned to the exact connector version and the exact test fixtures.

A community catalog can *say* "certified"; it cannot *sign* it. That signature
is the F-Pulse+ differentiator.

OSS / Plus split (honest):
  * VERIFYING an attestation is free and offline — shipped in OSS
    (`verify_attestation`, `verify_connector`, `fpulse verify-connector`).
    The whole value of a signature is that recipients can check it themselves.
  * ISSUING (signing) requires the private issuing key, which OSS does not
    ship. `attest_connector` refuses without one. In F-Pulse+ the key is
    provisioned and customers pin its public half as the trusted authority.
    (The signing *code* lives here so the mechanism is inspectable and
    testable; the *authority* — which public key is trusted — is the Plus gate.)

Envelope shape (what gets written to connectors/attestations/<id>.json):

    {
      "attestation": {                      # the signed body (canonicalised)
        "kind": "fpulse.connector.verified",
        "spec_version": 1,
        "connector": "github",
        "cat_level": "verified",
        "failed": 0,
        "manifest_sha256": "…",
        "fixture_sha256": {"repos.happy_path.cassette.json": "…", …},
        "cat_run_at": "…",
        "issued_at": "…"
      },
      "signature": {
        "alg": "ed25519",
        "key_id": "<sha256(pubkey)[:16]>",
        "public_key": "<hex of 32-byte raw public key>",
        "value": "<hex of 64-byte signature over canonical(attestation)>"
      }
    }
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from fpulse.connectors.cat import CatReport, run_cat

_CONNECTORS_DIR = Path(__file__).resolve().parent
ATTEST_DIR = _CONNECTORS_DIR / "attestations"

_ATTESTABLE_LEVELS = ("verified", "production")


class AttestationError(Exception):
    """Raised when an attestation cannot be issued or a key is missing."""


# ── Canonicalisation ──────────────────────────────────────────────────────

def _canonical(body: dict[str, Any]) -> bytes:
    """Deterministic bytes for signing/verifying — stable key order, no spaces."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _key_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


# ── Key handling ──────────────────────────────────────────────────────────

def generate_keypair() -> tuple[str, str]:
    """Return (private_key_hex, public_key_hex) for a fresh Ed25519 key.

    Both are hex of the 32-byte raw keys. Store the private half somewhere
    only the issuer controls (never in the OSS repo); publish the public half
    as the trusted authority customers pin.
    """
    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv_raw.hex(), pub_raw.hex()


def _load_private_key(source: str | bytes | None) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from `source` or FPULSE_ATTEST_PRIVATE_KEY.

    Accepts: 64-char hex (raw 32-byte key), a PEM string, or a path to a file
    containing either. Raises AttestationError when nothing usable is found —
    this is the signing gate (OSS ships no key).
    """
    raw = source if source is not None else os.environ.get("FPULSE_ATTEST_PRIVATE_KEY")
    if not raw:
        raise AttestationError(
            "no signing key — set FPULSE_ATTEST_PRIVATE_KEY or pass one. "
            "Issuing signed 'Verified' attestations is an F-Pulse+ capability; "
            "the public verifier is free."
        )
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "ignore") if not _looks_raw(raw) else None
        if text is None:
            return Ed25519PrivateKey.from_private_bytes(raw)
        raw = text
    raw = raw.strip()
    # A filesystem path?
    if os.path.sep in raw or raw.endswith((".pem", ".key", ".hex")):
        p = Path(raw)
        if p.is_file():
            raw = p.read_text(encoding="utf-8").strip()
    # PEM?
    if "BEGIN" in raw:
        key = serialization.load_pem_private_key(raw.encode("utf-8"), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise AttestationError("PEM key is not an Ed25519 private key")
        return key
    # Hex of raw 32 bytes.
    try:
        return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))
    except (ValueError, TypeError) as exc:
        raise AttestationError(f"unrecognised private key format: {exc}") from exc


def _looks_raw(b: bytes) -> bool:
    return len(b) == 32 and not b.lstrip().startswith(b"-----")


def _public_hex(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


# ── Build + sign ──────────────────────────────────────────────────────────

def build_attestation_body(connector_id: str, *, report: CatReport | None = None,
                           now: datetime | None = None) -> dict[str, Any]:
    """Assemble the (unsigned) attestation body from a CAT run."""
    r = report if report is not None else run_cat(connector_id, mode="replay")
    issued = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "kind": "fpulse.connector.verified",
        "spec_version": 1,
        "connector": connector_id,
        "cat_level": r.cat_level,
        "failed": r.failed,
        "manifest_sha256": r.manifest_sha256,
        "fixture_sha256": dict(sorted(r.fixture_sha256.items())),
        "cat_run_at": r.run_at,
        "issued_at": issued,
    }


def sign_body(body: dict[str, Any], private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Sign an attestation body, returning the full envelope."""
    pub_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    signature = private_key.sign(_canonical(body))
    return {
        "attestation": body,
        "signature": {
            "alg": "ed25519",
            "key_id": _key_id(pub_raw),
            "public_key": pub_raw.hex(),
            "value": signature.hex(),
        },
    }


def attest_connector(connector_id: str, *, private_key_source: str | bytes | None = None,
                     report: CatReport | None = None) -> dict[str, Any]:
    """Build + sign an attestation for a connector that passed CAT.

    Refuses to sign a connector whose CAT suite is not green (failed > 0 or a
    level below `verified`) — a signature over a red run would be worse than
    none. Raises AttestationError without a signing key (the Plus gate).
    """
    r = report if report is not None else run_cat(connector_id, mode="replay")
    if r.failed != 0 or r.cat_level not in _ATTESTABLE_LEVELS:
        raise AttestationError(
            f"refusing to attest '{connector_id}': CAT not green "
            f"(level={r.cat_level}, failed={r.failed}). Fix the suite first."
        )
    priv = _load_private_key(private_key_source)
    body = build_attestation_body(connector_id, report=r)
    return sign_body(body, priv)


# ── Verify ────────────────────────────────────────────────────────────────

def verify_attestation(envelope: dict[str, Any], *,
                       trusted_public_key: str | None = None) -> tuple[bool, str]:
    """Verify an attestation envelope offline.

    Returns (ok, reason). When `trusted_public_key` (hex) is given, the
    signature must have been produced by that exact key — otherwise a valid
    signature from an UNKNOWN key is reported as untrusted (valid math, wrong
    issuer). Without a pin, self-consistency is verified and the key_id is
    reported so the caller can decide whether they trust it.
    """
    if not isinstance(envelope, dict):
        return False, "not an attestation object"
    body = envelope.get("attestation")
    sig = envelope.get("signature")
    if not isinstance(body, dict) or not isinstance(sig, dict):
        return False, "malformed envelope (missing attestation/signature)"
    if sig.get("alg") != "ed25519":
        return False, f"unsupported signature alg {sig.get('alg')!r}"
    pub_hex = str(sig.get("public_key") or "")
    value_hex = str(sig.get("value") or "")
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        signature = bytes.fromhex(value_hex)
    except (ValueError, TypeError) as exc:
        return False, f"unreadable key/signature: {exc}"

    key_id = _key_id(bytes.fromhex(pub_hex))
    try:
        pub.verify(signature, _canonical(body))
    except InvalidSignature:
        return False, "signature INVALID — attestation was tampered with or re-serialised"

    if trusted_public_key:
        if trusted_public_key.strip().lower() != pub_hex.lower():
            return False, (f"signature is cryptographically valid but signed by an "
                          f"UNTRUSTED key (key_id {key_id}); expected your pinned key")
        return True, f"signature valid and issued by your pinned key (key_id {key_id})"
    return True, f"signature valid (ed25519, key_id {key_id}; no trust-pin supplied)"


# ── Store + connector-level helpers ───────────────────────────────────────

def attestation_path(connector_id: str) -> Path:
    return ATTEST_DIR / f"{connector_id}.json"


def write_attestation(envelope: dict[str, Any], out_path: str | os.PathLike | None = None,
                      *, connector_id: str | None = None) -> Path:
    cid = connector_id or envelope.get("attestation", {}).get("connector")
    p = Path(out_path) if out_path else attestation_path(str(cid))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(envelope, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def load_attestation(connector_id: str, *, path: str | os.PathLike | None = None) -> dict[str, Any] | None:
    p = Path(path) if path else attestation_path(connector_id)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def verify_connector(connector_id: str, *, path: str | os.PathLike | None = None,
                     trusted_public_key: str | None = None
                     ) -> tuple[bool, str, dict[str, Any] | None]:
    """Load + verify a connector's committed attestation. (ok, reason, envelope)."""
    env = load_attestation(connector_id, path=path)
    if env is None:
        return False, f"no attestation found for '{connector_id}'", None
    pin = trusted_public_key or os.environ.get("FPULSE_ATTEST_PUBLIC_KEY")
    ok, reason = verify_attestation(env, trusted_public_key=pin)
    return ok, reason, env


# ── CLI: python -m fpulse.connectors.attest {keygen|attest|verify} ─────────

def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="fpulse-attest",
                                 description="Sign / verify connector Verified attestations.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("keygen", help="Generate an Ed25519 keypair (hex)")
    a = sub.add_parser("attest", help="Sign an attestation for a green connector (needs a key)")
    a.add_argument("connector_id")
    a.add_argument("--key", help="Private key (hex/PEM/path); else FPULSE_ATTEST_PRIVATE_KEY")
    a.add_argument("--out", help="Output path (default connectors/attestations/<id>.json)")
    v = sub.add_parser("verify", help="Verify a connector's committed attestation")
    v.add_argument("connector_id")
    v.add_argument("--file", help="Attestation file (default connectors/attestations/<id>.json)")
    v.add_argument("--pubkey", help="Trusted public key hex to pin (else FPULSE_ATTEST_PUBLIC_KEY)")
    args = ap.parse_args(argv)

    if args.cmd == "keygen":
        priv, pub = generate_keypair()
        print(f"private_key_hex: {priv}")
        print(f"public_key_hex:  {pub}")
        print("Keep the private key secret (never commit it). Publish/pin the public key.")
        return 0

    if args.cmd == "attest":
        try:
            env = attest_connector(args.connector_id, private_key_source=args.key)
        except AttestationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        p = write_attestation(env, args.out, connector_id=args.connector_id)
        s = env["signature"]
        print(f"Signed '{args.connector_id}' → {p}")
        print(f"  cat_level: {env['attestation']['cat_level']}  key_id: {s['key_id']}")
        return 0

    if args.cmd == "verify":
        ok, reason, env = verify_connector(
            args.connector_id, path=args.file, trusted_public_key=args.pubkey)
        if env is not None:
            b = env["attestation"]
            print(f"Connector:  {b['connector']}")
            print(f"CAT level:  {b['cat_level']}  (failed={b['failed']})")
            print(f"Manifest:   sha256:{b['manifest_sha256'][:16]}…")
            print(f"Issued:     {b.get('issued_at')}")
        print(f"Result:     {'✓ VALID — ' if ok else '✗ '}{reason}")
        return 0 if ok else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main())
