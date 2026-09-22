"""Tests for src/credential_encryption.py -- server-side symmetric
encryption for per-customer Polymarket credentials at rest. Security-
critical: nothing here should ever leak plaintext, ciphertext, or the
master key into logs or exception messages."""

from __future__ import annotations

import pytest

from src.credential_encryption import (
    ENCRYPTION_KEY_ENV_VAR,
    CredentialEncryptionError,
    credential_set_fingerprint,
    decrypt_secret,
    encrypt_secret,
    fingerprint_secret,
    generate_encryption_key,
    main,
)


@pytest.fixture
def encryption_key(monkeypatch):
    key = generate_encryption_key()
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, key)
    return key


class TestRoundTrip:
    def test_encrypt_then_decrypt_returns_original(self, encryption_key):
        ct = encrypt_secret("super-secret-private-key-material")
        assert decrypt_secret(ct) == "super-secret-private-key-material"

    def test_ciphertext_never_contains_the_plaintext(self, encryption_key):
        secret = "MySecretApiKeyId12345"
        ct = encrypt_secret(secret)
        assert secret not in ct

    def test_two_encryptions_of_the_same_plaintext_differ(self, encryption_key):
        """Fernet includes a random IV -- ciphertext must not be a
        deterministic function of plaintext alone (would leak equality
        between two customers' credentials)."""
        ct1 = encrypt_secret("same-value")
        ct2 = encrypt_secret("same-value")
        assert ct1 != ct2
        assert decrypt_secret(ct1) == decrypt_secret(ct2) == "same-value"

    def test_empty_plaintext_rejected(self, encryption_key):
        with pytest.raises(ValueError):
            encrypt_secret("")


class TestMissingOrBadKey:
    def test_missing_env_var_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv(ENCRYPTION_KEY_ENV_VAR, raising=False)
        with pytest.raises(CredentialEncryptionError, match=ENCRYPTION_KEY_ENV_VAR):
            encrypt_secret("x")

    def test_malformed_env_var_raises_clear_error(self, monkeypatch):
        monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, "not-valid-base64!!!")
        with pytest.raises(CredentialEncryptionError):
            encrypt_secret("x")

    def test_wrong_length_key_rejected(self, monkeypatch):
        import base64
        monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, base64.urlsafe_b64encode(b"too-short").decode())
        with pytest.raises(CredentialEncryptionError):
            encrypt_secret("x")

    def test_decrypting_with_a_different_key_fails_cleanly(self, monkeypatch):
        monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())
        ct = encrypt_secret("secret")
        monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())
        with pytest.raises(CredentialEncryptionError):
            decrypt_secret(ct)

    def test_decrypting_garbage_fails_cleanly_not_a_raw_library_exception(self, encryption_key):
        with pytest.raises(CredentialEncryptionError):
            decrypt_secret("this-is-not-a-real-fernet-token")


class TestNoSecretLeakage:
    """The core security guarantee: no exception message, log record,
    or return value from this module ever contains plaintext,
    ciphertext, or the master key."""

    def test_missing_key_error_message_never_contains_a_real_key_value(self, monkeypatch):
        monkeypatch.delenv(ENCRYPTION_KEY_ENV_VAR, raising=False)
        try:
            encrypt_secret("some-plaintext-secret")
        except CredentialEncryptionError as exc:
            assert "some-plaintext-secret" not in str(exc)

    def test_decrypt_failure_error_message_never_contains_the_ciphertext(self, encryption_key):
        bad_ciphertext = "gAAAAABnot-a-real-token-but-looks-like-one"
        try:
            decrypt_secret(bad_ciphertext)
        except CredentialEncryptionError as exc:
            assert bad_ciphertext not in str(exc)

    def test_log_records_never_contain_plaintext_or_ciphertext(self, encryption_key, caplog):
        secret = "MyVeryRealPolymarketPrivateKey"
        ct = encrypt_secret(secret)
        try:
            decrypt_secret("garbage-token")
        except CredentialEncryptionError:
            pass
        for record in caplog.records:
            msg = record.getMessage()
            assert secret not in msg
            assert ct not in msg


class TestFingerprint:
    def test_fingerprint_is_last_four_chars(self):
        assert fingerprint_secret("AbCdEfGhA7F3") == "A7F3"

    def test_fingerprint_of_empty_string_is_empty(self):
        assert fingerprint_secret("") == ""

    def test_fingerprint_never_contains_the_full_secret(self):
        secret = "short"
        fp = fingerprint_secret(secret)
        assert fp != secret
        assert len(fp) <= 4

    def test_credential_set_fingerprint_is_stable_for_same_inputs(self):
        a = credential_set_fingerprint("key1", "key2")
        b = credential_set_fingerprint("key1", "key2")
        assert a == b

    def test_credential_set_fingerprint_changes_when_inputs_change(self):
        a = credential_set_fingerprint("key1", "key2")
        b = credential_set_fingerprint("key1", "key2-changed")
        assert a != b

    def test_credential_set_fingerprint_is_not_reversible_to_inputs(self):
        fp = credential_set_fingerprint("my-real-api-key-id", "my-real-private-key")
        assert "my-real-api-key-id" not in fp
        assert "my-real-private-key" not in fp


class TestGenerateKey:
    def test_generated_key_is_usable_immediately(self, monkeypatch):
        key = generate_encryption_key()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, key)
        ct = encrypt_secret("test")
        assert decrypt_secret(ct) == "test"

    def test_generated_keys_are_unique(self):
        assert generate_encryption_key() != generate_encryption_key()


class TestCliMain:
    def test_generate_key_command_prints_a_usable_key(self, capsys, monkeypatch):
        exit_code = main(["generate-key"])
        assert exit_code == 0
        printed = capsys.readouterr().out.strip()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, printed)
        ct = encrypt_secret("test")
        assert decrypt_secret(ct) == "test"

    def test_unknown_command_exits_nonzero(self, capsys):
        exit_code = main(["bogus"])
        assert exit_code == 1
