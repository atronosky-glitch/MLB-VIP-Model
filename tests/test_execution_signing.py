"""Tests for src/execution/signing.py. Uses throwaway keypairs generated
in-test -- never touches real credentials."""

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from src.execution.signing import build_signed_message, sign_ed25519, sign_rsa_pss

import base64


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def ed25519_keypair():
    private_key = ed25519.Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


class TestBuildSignedMessage:
    def test_concatenates_with_no_separator(self):
        msg = build_signed_message("1234567890", "GET", "/trade-api/v2/portfolio/balance")
        assert msg == b"1234567890GET/trade-api/v2/portfolio/balance"

    def test_returns_bytes(self):
        assert isinstance(build_signed_message("1", "GET", "/x"), bytes)


class TestSignRsaPss:
    def test_signature_verifies_against_the_public_key(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        message = b"1234567890GET/trade-api/v2/portfolio/balance"
        signature_b64 = sign_rsa_pss(private_key, message)

        # Independent verification path -- not a self-round-trip through
        # our own signing code.
        public_key.verify(
            base64.b64decode(signature_b64),
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )

    def test_tampered_message_fails_verification(self, rsa_keypair):
        private_key, public_key = rsa_keypair
        message = b"1234567890GET/trade-api/v2/portfolio/balance"
        signature_b64 = sign_rsa_pss(private_key, message)

        from cryptography.exceptions import InvalidSignature
        with pytest.raises(InvalidSignature):
            public_key.verify(
                base64.b64decode(signature_b64),
                message + b"tampered",
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
                hashes.SHA256(),
            )

    def test_output_is_base64_text_not_raw_key_material(self, rsa_keypair):
        private_key, _ = rsa_keypair
        signature_b64 = sign_rsa_pss(private_key, b"some message")
        assert isinstance(signature_b64, str)
        # valid base64
        base64.b64decode(signature_b64, validate=True)


class TestSignEd25519:
    def test_signature_verifies_against_the_public_key(self, ed25519_keypair):
        private_key, public_key = ed25519_keypair
        message = b"1234567890GET/v1/account/balances"
        signature_b64 = sign_ed25519(private_key, message)

        public_key.verify(base64.b64decode(signature_b64), message)

    def test_tampered_message_fails_verification(self, ed25519_keypair):
        private_key, public_key = ed25519_keypair
        message = b"1234567890GET/v1/account/balances"
        signature_b64 = sign_ed25519(private_key, message)

        from cryptography.exceptions import InvalidSignature
        with pytest.raises(InvalidSignature):
            public_key.verify(base64.b64decode(signature_b64), message + b"tampered")

    def test_output_is_base64_text(self, ed25519_keypair):
        private_key, _ = ed25519_keypair
        signature_b64 = sign_ed25519(private_key, b"some message")
        assert isinstance(signature_b64, str)
        base64.b64decode(signature_b64, validate=True)
