"""Private-key loading for the execution layer.

Hard rule: nothing in this module ever logs, returns in a string form, or
otherwise exposes key material. Every error path reports the file path
and exception type only. Centralized here (rather than duplicated per
provider) so that guarantee only needs to be tested once -- see
tests/test_execution_credentials.py.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

logger = logging.getLogger(__name__)


class CredentialLoadError(RuntimeError):
    """Raised when a private key file can't be read or parsed.

    Message includes the file path and underlying error type only --
    never the file's contents.
    """


def load_rsa_private_key(path: str | Path) -> rsa.RSAPrivateKey:
    """Load a Kalshi RSA private key from a PEM file at *path*."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        logger.error("Could not read RSA private key file %s: %s", p, type(exc).__name__)
        raise CredentialLoadError(f"Could not read RSA private key file {p}: {type(exc).__name__}") from exc

    try:
        key = serialization.load_pem_private_key(data, password=None)
    except Exception as exc:
        logger.error("Could not parse RSA private key file %s: %s", p, type(exc).__name__)
        raise CredentialLoadError(f"Could not parse RSA private key file {p}: {type(exc).__name__}") from exc

    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialLoadError(f"{p} does not contain an RSA private key")
    return key


def load_ed25519_private_key(path: str | Path) -> ed25519.Ed25519PrivateKey:
    """Load a Polymarket US Ed25519 private key from *path*.

    Per docs.polymarket.us: the secret key is a base64-encoded string
    that decodes to 32 raw bytes (the seed). Also accepts a 64-byte
    decode -- the common NaCl/libsodium "secret key" export convention
    (32-byte seed followed by its own 32-byte derived public key,
    concatenated) that some Polymarket US accounts' downloaded keys use
    in practice -- confirmed 2026-09-14 against a real downloaded key: a
    64-byte decode's first 32 bytes derive exactly its own last 32 bytes
    as an Ed25519 public key, which only a genuine seed+pubkey pair
    would do. Any other length is rejected outright, not guessed at.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.error("Could not read Ed25519 private key file %s: %s", p, type(exc).__name__)
        raise CredentialLoadError(f"Could not read Ed25519 private key file {p}: {type(exc).__name__}") from exc

    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception as exc:
        logger.error("Could not base64-decode Ed25519 private key file %s: %s", p, type(exc).__name__)
        raise CredentialLoadError(f"Could not base64-decode Ed25519 private key file {p}: {type(exc).__name__}") from exc

    if len(decoded) == 32:
        return ed25519.Ed25519PrivateKey.from_private_bytes(decoded)

    if len(decoded) == 64:
        seed, claimed_pub = decoded[:32], decoded[32:]
        key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
        derived_pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
        )
        if derived_pub != claimed_pub:
            raise CredentialLoadError(
                f"Ed25519 private key file {p} decoded to 64 bytes, but the second half is not "
                "the Ed25519 public key derived from the first half (not a valid seed+pubkey pair)"
            )
        return key

    raise CredentialLoadError(
        f"Ed25519 private key file {p} decoded to {len(decoded)} bytes, expected 32 (raw seed) "
        "or 64 (seed + derived public key)"
    )
