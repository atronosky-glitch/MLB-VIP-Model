"""Tests for src/customer_kalshi.py -- per-customer Kalshi Auto-Bet
connection: verify, connect, disconnect, enable/disable Auto-Bet, risk
settings. All Kalshi network calls are mocked -- these tests never
touch the real API. Deliberate structural mirror of
tests/test_customer_polymarket.py -- see that file for the shared
design rationale; only the credential shape (RSA/PEM vs Ed25519/base64)
differs."""

from __future__ import annotations

from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import src.customer_kalshi as ck
from src.credential_encryption import ENCRYPTION_KEY_ENV_VAR, generate_encryption_key


def _real_pem_key() -> str:
    """A real, validly-generated RSA private key, PEM-encoded -- exactly
    the shape parse_rsa_private_key_material requires. Each call
    produces a distinct key. 2048 bits (not Kalshi's likely real size,
    unconfirmed) is plenty for a throwaway test key and keeps generation
    fast."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("ascii")


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())


def _ok_health_check():
    result = mock.MagicMock()
    result.ok = True
    result.detail = "balance fetched successfully"
    return result


def _failed_health_check():
    result = mock.MagicMock()
    result.ok = False
    result.detail = "PermissionError: Kalshi rejected credentials (HTTP 401) for key_id=****xyz"
    return result


class TestVerifyCredentials:
    def test_empty_fields_rejected_without_a_network_call(self):
        ok, msg = ck.verify_credentials("", "")
        assert ok is False
        assert "required" in msg.lower()

    def test_valid_credentials_verify_ok(self):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ok, msg = ck.verify_credentials("real-key-id", _real_pem_key())
        assert ok is True

    def test_invalid_key_material_rejected_with_a_safe_message(self):
        ok, msg = ck.verify_credentials("real-key-id", "not a valid pem key!!!")
        assert ok is False
        assert "Private Key" in msg

    def test_provider_rejection_returns_a_safe_generic_message(self):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_failed_health_check()):
            ok, msg = ck.verify_credentials("real-key-id", _real_pem_key())
        assert ok is False
        assert "PermissionError" not in msg
        assert "401" not in msg

    def test_network_exception_never_crashes_returns_safe_message(self):
        with mock.patch.object(ck.KalshiProvider, "health_check", side_effect=RuntimeError("boom")):
            ok, msg = ck.verify_credentials("real-key-id", _real_pem_key())
        assert ok is False
        assert "boom" not in msg


class TestConnectAccount:
    def test_successful_connect_persists_encrypted_credentials(self, db_conn):
        private_key_pem = _real_pem_key()
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ok, msg = ck.connect_account(db_conn, "acct-1", "real-key-id", private_key_pem)
        assert ok is True
        row = dict(db_conn.execute(
            "SELECT * FROM customer_kalshi_accounts WHERE account_id = 'acct-1'"
        ).fetchone())
        assert row["kalshi_connected"] == 1
        assert row["encrypted_api_key_id"] is not None
        assert row["encrypted_api_key_id"] != "real-key-id"  # never stored as plaintext
        assert row["encrypted_private_key"] != private_key_pem

    def test_failed_verify_does_not_persist_anything_decryptable(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_failed_health_check()):
            ok, msg = ck.connect_account(db_conn, "acct-1", "bad-key", _real_pem_key())
        assert ok is False
        row = db_conn.execute(
            "SELECT * FROM customer_kalshi_accounts WHERE account_id = 'acct-1'"
        ).fetchone()
        assert dict(row)["kalshi_connected"] == 0
        assert dict(row)["encrypted_api_key_id"] is None

    def test_connecting_never_enables_autobet(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "real-key-id", _real_pem_key())
        status = ck.get_status(db_conn, "acct-1")
        assert status.connected is True
        assert status.autobet_enabled is False

    def test_replacing_credentials_resets_autobet_to_off(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "key-1", _real_pem_key())
            ck.save_risk_settings(db_conn, "acct-1", **ck.DEFAULT_RISK_SETTINGS)
            ck.enable_autobet(db_conn, "acct-1")
            assert ck.get_status(db_conn, "acct-1").autobet_enabled is True

            ck.connect_account(db_conn, "acct-1", "key-2", _real_pem_key())
        assert ck.get_status(db_conn, "acct-1").autobet_enabled is False

    def test_status_never_exposes_the_real_api_key(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "super-secret-real-key-id", _real_pem_key())
        status = ck.get_status(db_conn, "acct-1")
        assert "super-secret-real-key-id" not in (status.api_key_id_display or "")
        assert status.api_key_id_display.startswith("••••••••")

    def test_failed_replace_attempt_does_not_disturb_a_working_connection(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "good-key", _real_pem_key())
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_failed_health_check()):
            ok, _ = ck.connect_account(db_conn, "acct-1", "bad-key", _real_pem_key())
        assert ok is False
        creds = ck.get_decrypted_credentials(db_conn, "acct-1")
        assert creds is not None
        assert creds[0] == "good-key"


class TestGetStatus:
    def test_never_connected_returns_a_clean_default(self, db_conn):
        status = ck.get_status(db_conn, "never-connected-acct")
        assert status.connected is False
        assert status.api_key_id_display is None
        assert status.autobet_enabled is False
        assert status.live_execution is False


class TestGetDecryptedCredentials:
    def test_returns_none_when_not_connected(self, db_conn):
        assert ck.get_decrypted_credentials(db_conn, "acct-1") is None

    def test_returns_the_real_values_when_connected(self, db_conn):
        private_key_pem = _real_pem_key()
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", private_key_pem)
        creds = ck.get_decrypted_credentials(db_conn, "acct-1")
        assert creds == ("my-key-id", private_key_pem)

    def test_returns_none_after_disconnect(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.disconnect_account(db_conn, "acct-1")
        assert ck.get_decrypted_credentials(db_conn, "acct-1") is None


class TestDisconnectAccount:
    def test_wipes_credentials_and_disables_autobet(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
            ck.save_risk_settings(db_conn, "acct-1", **ck.DEFAULT_RISK_SETTINGS)
            ck.enable_autobet(db_conn, "acct-1")
        ck.disconnect_account(db_conn, "acct-1")
        status = ck.get_status(db_conn, "acct-1")
        assert status.connected is False
        assert status.autobet_enabled is False
        row = dict(db_conn.execute(
            "SELECT * FROM customer_kalshi_accounts WHERE account_id = 'acct-1'"
        ).fetchone())
        assert row["encrypted_api_key_id"] is None
        assert row["encrypted_private_key"] is None

    def test_disconnect_of_never_connected_account_is_a_safe_noop(self, db_conn):
        ck.disconnect_account(db_conn, "never-connected")  # must not raise


class TestEnableAutobet:
    def test_fails_when_not_connected(self, db_conn):
        ok, msg = ck.enable_autobet(db_conn, "acct-1")
        assert ok is False
        assert "connect" in msg.lower()

    def test_fails_when_risk_settings_incomplete(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ok, msg = ck.enable_autobet(db_conn, "acct-1")
        assert ok is False
        assert "risk" in msg.lower()

    def test_succeeds_once_connected_and_configured(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.save_risk_settings(db_conn, "acct-1", **ck.DEFAULT_RISK_SETTINGS)
        ok, msg = ck.enable_autobet(db_conn, "acct-1")
        assert ok is True
        assert ck.get_status(db_conn, "acct-1").autobet_enabled is True

    def test_partial_risk_settings_still_blocks_enable(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.save_risk_settings(db_conn, "acct-1", unit_size_usd=5.0)
        ok, msg = ck.enable_autobet(db_conn, "acct-1")
        assert ok is False


class TestDisableAutobet:
    def test_disable_stops_autobet(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.save_risk_settings(db_conn, "acct-1", **ck.DEFAULT_RISK_SETTINGS)
        ck.enable_autobet(db_conn, "acct-1")
        ck.disable_autobet(db_conn, "acct-1")
        assert ck.get_status(db_conn, "acct-1").autobet_enabled is False

    def test_disable_does_not_disconnect_the_account(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.save_risk_settings(db_conn, "acct-1", **ck.DEFAULT_RISK_SETTINGS)
        ck.enable_autobet(db_conn, "acct-1")
        ck.disable_autobet(db_conn, "acct-1")
        assert ck.get_status(db_conn, "acct-1").connected is True


class TestLiveExecutionToggle:
    def test_defaults_to_paper(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        assert ck.get_status(db_conn, "acct-1").live_execution is False

    def test_can_be_set_to_live_explicitly(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.set_live_execution(db_conn, "acct-1", True)
        assert ck.get_status(db_conn, "acct-1").live_execution is True

    def test_disconnect_resets_live_execution_to_false(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.set_live_execution(db_conn, "acct-1", True)
        ck.disconnect_account(db_conn, "acct-1")
        assert ck.get_status(db_conn, "acct-1").live_execution is False


class TestRiskSettings:
    def test_defaults_are_all_none_before_connecting(self, db_conn):
        settings = ck.get_risk_settings(db_conn, "never-connected")
        assert all(v is None for v in settings.values())

    def test_save_then_read_round_trips(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        ck.save_risk_settings(db_conn, "acct-1", unit_size_usd=7.5, max_bet_usd=30.0)
        settings = ck.get_risk_settings(db_conn, "acct-1")
        assert settings["unit_size_usd"] == 7.5
        assert settings["max_bet_usd"] == 30.0

    def test_unknown_field_raises_rather_than_silently_ignored(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "my-key-id", _real_pem_key())
        with pytest.raises(ValueError):
            ck.save_risk_settings(db_conn, "acct-1", not_a_real_field=1)

    def test_saving_before_connecting_raises(self, db_conn):
        with pytest.raises(ValueError):
            ck.save_risk_settings(db_conn, "never-connected", unit_size_usd=5.0)


class TestUserIsolation:
    def test_two_accounts_credentials_are_fully_independent(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-A", "key-A", _real_pem_key())
            ck.connect_account(db_conn, "acct-B", "key-B", _real_pem_key())
        creds_a = ck.get_decrypted_credentials(db_conn, "acct-A")
        creds_b = ck.get_decrypted_credentials(db_conn, "acct-B")
        assert creds_a[0] == "key-A"
        assert creds_b[0] == "key-B"
        assert creds_a != creds_b

    def test_disabling_one_account_does_not_affect_another(self, db_conn):
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-A", "key-A", _real_pem_key())
            ck.connect_account(db_conn, "acct-B", "key-B", _real_pem_key())
        ck.save_risk_settings(db_conn, "acct-A", **ck.DEFAULT_RISK_SETTINGS)
        ck.save_risk_settings(db_conn, "acct-B", **ck.DEFAULT_RISK_SETTINGS)
        ck.enable_autobet(db_conn, "acct-A")
        ck.enable_autobet(db_conn, "acct-B")
        ck.disable_autobet(db_conn, "acct-A")
        assert ck.get_status(db_conn, "acct-A").autobet_enabled is False
        assert ck.get_status(db_conn, "acct-B").autobet_enabled is True


class TestKalshiPolymarketAreFullyIndependent:
    """A customer's Kalshi connection/settings must never interact with
    their Polymarket connection/settings -- separate tables, separate
    everything (see src/customer_kalshi.py's module docstring)."""

    def test_connecting_kalshi_does_not_touch_polymarket_status(self, db_conn):
        import src.customer_polymarket as cp
        with mock.patch.object(ck.KalshiProvider, "health_check", return_value=_ok_health_check()):
            ck.connect_account(db_conn, "acct-1", "kalshi-key", _real_pem_key())
        assert cp.get_status(db_conn, "acct-1").connected is False
        assert ck.get_status(db_conn, "acct-1").connected is True
