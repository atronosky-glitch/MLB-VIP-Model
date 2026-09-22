"""Server-side symmetric encryption for customer-supplied secrets at
rest (currently: per-customer Polymarket US API credentials -- see
src/customer_polymarket.py).

Fernet (AES-128-CBC + HMAC-SHA256, authenticated) via the
`cryptography` package, already a dependency (see
src/execution/credentials.py's RSA/Ed25519 usage). The master key comes
from the POLYMARKET_CREDENTIAL_ENCRYPTION_KEY Render environment
variable -- never committed, never logged, never returned from any
function here. Only ciphertext this module produced is meant to touch
the database; plaintext exists in memory only for the instant a caller
needs it.

Hard rule, matching src/execution/credentials.py's own convention:
nothing in this module ever logs, returns, or otherwise exposes
plaintext, the master key, or a ciphertext value in an exception
message. Errors report the failure *kind* only.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV_VAR = "POLYMARKET_CREDENTIAL_ENCRYPTION_KEY"


class CredentialEncryptionError(RuntimeError):
    """Raised when encryption/decryption fails or the master key is
    missing/malformed. Message never includes plaintext, ciphertext,
    or the master key itself -- only the failure kind."""


def _get_master_key() -> bytes:
    raw = os.environ.get(ENCRYPTION_KEY_ENV_VAR, "")
    if not raw:
        raise CredentialEncryptionError(
            f"{ENCRYPTION_KEY_ENV_VAR} is not set -- cannot encrypt/decrypt customer credentials"
        )
    # A Fernet key is itself a 32-byte value, urlsafe-base64-encoded --
    # validated here so a malformed env var fails with a clear message
    # rather than a confusing error from deep inside Fernet's own
    # constructor.
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except Exception as exc:
        raise CredentialEncryptionError(
            f"{ENCRYPTION_KEY_ENV_VAR} is not valid urlsafe-base64: {type(exc).__name__}"
        ) from exc
    if len(decoded) != 32:
        raise CredentialEncryptionError(
            f"{ENCRYPTION_KEY_ENV_VAR} must decode to exactly 32 bytes, got {len(decoded)}"
        )
    return raw.encode("ascii")


def generate_encryption_key() -> str:
    """Generate a new, valid POLYMARKET_CREDENTIAL_ENCRYPTION_KEY value
    -- an operator-facing helper (see `python -m src.credential_encryption
    generate-key`), never called automatically. Rotating this key makes
    every previously-encrypted credential undecryptable -- customers
    would need to reconnect their Polymarket account."""
    return Fernet.generate_key().decode("ascii")


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* with the server master key. Returns an
    opaque ciphertext string safe to store in the database."""
    if not plaintext:
        raise ValueError("plaintext must be non-empty")
    token = Fernet(_get_master_key()).encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a value produced by encrypt_secret. Raises
    CredentialEncryptionError -- never the underlying InvalidToken (or
    any other library exception whose str() could in principle include
    fragments of the input) -- on failure."""
    try:
        plaintext = Fernet(_get_master_key()).decrypt(ciphertext.encode("ascii"))
    except CredentialEncryptionError:
        raise
    except InvalidToken as exc:
        logger.error("Failed to decrypt a stored credential: invalid token or wrong master key")
        raise CredentialEncryptionError("Failed to decrypt stored credential (invalid token or wrong key)") from exc
    except Exception as exc:
        logger.error("Failed to decrypt a stored credential: %s", type(exc).__name__)
        raise CredentialEncryptionError(f"Failed to decrypt stored credential: {type(exc).__name__}") from exc
    return plaintext.decode("utf-8")


def fingerprint_secret(plaintext: str) -> str:
    """A short, safe-to-display suffix (e.g. "API Key: ****A7F3") --
    never enough to reconstruct or meaningfully narrow down the real
    value."""
    if not plaintext:
        return ""
    return plaintext[-4:]


def credential_set_fingerprint(*values: str) -> str:
    """A stable, irreversible hash across multiple credential fields
    (e.g. api_key_id + private_key together), used only to detect
    whether a customer's stored credentials changed on a later
    "Replace Credentials" -- never displayed to the user, never logged
    alongside the real values."""
    h = hashlib.sha256()
    for v in values:
        h.update(v.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def main(argv: list[str] | None = None) -> int:
    """`python -m src.credential_encryption generate-key` -- an
    operator-facing one-shot to produce a value for
    POLYMARKET_CREDENTIAL_ENCRYPTION_KEY. Never called by any
    production code path."""
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if args == ["generate-key"]:
        print(generate_encryption_key())
        return 0
    print("usage: python -m src.credential_encryption generate-key", file=sys.stderr)
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
