"""Request-signing primitives for Kalshi (RSA-PSS) and Polymarket US
(Ed25519). Pure functions only -- no network or file I/O, no config
knowledge -- so they're testable against a throwaway keypair independent
of any provider or credential-loading code.

Both providers sign the identical message shape: the request timestamp
(milliseconds), HTTP method, and URL path (no host, no query string)
concatenated with no separator, then base64-encode the raw signature
bytes. Verified against each provider's current official docs
(docs.kalshi.com, docs.polymarket.us) on 2026-09-12 -- see the Stage 1
plan for exact citations.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

# Kalshi's documented PSS parameters: SHA-256 for both the message hash and
# the MGF1 mask, salt length equal to the digest length (32 bytes for
# SHA-256). Named here as a single constant so a live-server rejection
# (something a unit test can't catch -- see the Stage 1 plan's open
# decision #4) is a one-line fix, not a hunt through the signing call site.
_PSS_SALT_LENGTH = hashes.SHA256().digest_size


def build_signed_message(timestamp_ms: str, method: str, path: str) -> bytes:
    """The exact byte string both providers require you to sign."""
    return f"{timestamp_ms}{method}{path}".encode("utf-8")


def sign_rsa_pss(private_key: rsa.RSAPrivateKey, message: bytes) -> str:
    """Kalshi's signature scheme: RSA-PSS, SHA-256, base64-encoded."""
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=_PSS_SALT_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def sign_ed25519(private_key: ed25519.Ed25519PrivateKey, message: bytes) -> str:
    """Polymarket US's signature scheme: Ed25519, base64-encoded."""
    signature = private_key.sign(message)
    return base64.b64encode(signature).decode("ascii")
