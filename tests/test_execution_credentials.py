"""Tests for src/execution/credentials.py. Hard requirement under test:
nothing here ever logs or exposes key material, on any path."""

import base64
import logging

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from src.execution.credentials import (
    CredentialLoadError, load_ed25519_private_key, load_rsa_private_key,
)


@pytest.fixture
def rsa_pem_path(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "kalshi_key.pem"
    path.write_bytes(pem)
    return path, pem


@pytest.fixture
def ed25519_key_path(tmp_path):
    private_key = ed25519.Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    b64 = base64.b64encode(raw).decode("ascii")
    path = tmp_path / "polymarket_us_key.txt"
    path.write_text(b64)
    return path, b64


class TestLoadRsaPrivateKey:
    def test_loads_a_valid_pem_file(self, rsa_pem_path):
        path, _ = rsa_pem_path
        key = load_rsa_private_key(path)
        assert isinstance(key, rsa.RSAPrivateKey)

    def test_missing_file_raises_clear_error(self, tmp_path):
        with pytest.raises(CredentialLoadError):
            load_rsa_private_key(tmp_path / "does_not_exist.pem")

    def test_malformed_pem_raises_clear_error(self, tmp_path):
        path = tmp_path / "bad.pem"
        path.write_text("not a real pem file")
        with pytest.raises(CredentialLoadError):
            load_rsa_private_key(path)

    def test_no_log_record_ever_contains_key_material(self, rsa_pem_path, tmp_path, caplog):
        path, pem_bytes = rsa_pem_path
        pem_text = pem_bytes.decode("ascii")

        with caplog.at_level(logging.DEBUG):
            load_rsa_private_key(path)  # happy path
            try:
                load_rsa_private_key(tmp_path / "missing.pem")  # error path
            except CredentialLoadError:
                pass

        for record in caplog.records:
            assert "-----BEGIN" not in record.getMessage()
            assert pem_text not in record.getMessage()


class TestLoadEd25519PrivateKey:
    def test_loads_a_valid_base64_file(self, ed25519_key_path):
        path, _ = ed25519_key_path
        key = load_ed25519_private_key(path)
        assert isinstance(key, ed25519.Ed25519PrivateKey)

    def test_missing_file_raises_clear_error(self, tmp_path):
        with pytest.raises(CredentialLoadError):
            load_ed25519_private_key(tmp_path / "does_not_exist.txt")

    def test_invalid_base64_raises_clear_error(self, tmp_path):
        path = tmp_path / "bad.txt"
        path.write_text("not valid base64!!!")
        with pytest.raises(CredentialLoadError):
            load_ed25519_private_key(path)

    def test_wrong_decoded_length_raises_clear_error(self, tmp_path):
        # Valid base64, but decodes to fewer than 32 bytes.
        short = base64.b64encode(b"too short").decode("ascii")
        path = tmp_path / "short.txt"
        path.write_text(short)
        with pytest.raises(CredentialLoadError):
            load_ed25519_private_key(path)

    def test_no_log_record_ever_contains_key_material(self, ed25519_key_path, tmp_path, caplog):
        path, b64_text = ed25519_key_path

        with caplog.at_level(logging.DEBUG):
            load_ed25519_private_key(path)  # happy path
            try:
                load_ed25519_private_key(tmp_path / "missing.txt")  # error path
            except CredentialLoadError:
                pass

        for record in caplog.records:
            assert b64_text not in record.getMessage()
