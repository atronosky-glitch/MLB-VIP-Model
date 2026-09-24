"""Server-side validation of hostile Auto-Bet input (src/autobet_validation.py
and its use in customer_kalshi / customer_polymarket)."""

from __future__ import annotations

import base64
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

import src.customer_kalshi as ck
import src.customer_polymarket as cp
from src.autobet_validation import (
    RISK_BOUNDS, normalize_sport_filter, validate_credential_inputs, validate_risk_settings,
)
from src.credential_encryption import ENCRYPTION_KEY_ENV_VAR, generate_encryption_key
from src.customer_accounts import sign_up


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())


class TestRiskSettings:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -1, -0.01, 0, True, False,
                                     "10", None, [5], {"a": 1}, 10**12])
    def test_hostile_values_rejected_for_every_dollar_field(self, bad):
        for field in ("unit_size_usd", "max_bet_usd", "max_daily_loss_usd", "max_total_exposure_usd"):
            with pytest.raises(ValueError):
                validate_risk_settings({field: bad})

    def test_auto_approve_may_be_zero_but_not_negative_nan_or_huge(self):
        assert validate_risk_settings({"auto_approve_max_usd": 0})["auto_approve_max_usd"] == 0
        for bad in (-1, float("nan"), float("inf"), 5000.01):
            with pytest.raises(ValueError):
                validate_risk_settings({"auto_approve_max_usd": bad})

    def test_open_positions_must_be_a_whole_number_in_range(self):
        assert validate_risk_settings({"max_open_positions": 10.0})["max_open_positions"] == 10
        for bad in (0, 201, 2.5, float("nan"), True):
            with pytest.raises(ValueError):
                validate_risk_settings({"max_open_positions": bad})

    def test_boundaries_are_inclusive(self):
        for field, (lo, hi, _i, _m) in RISK_BOUNDS.items():
            assert validate_risk_settings({field: lo})[field] == lo
            assert validate_risk_settings({field: hi})[field] == hi

    def test_percentages_are_bounded(self):
        for field in ("min_net_ev_pct", "max_price_move_pct", "max_slippage_pct"):
            for bad in (-0.1, 100, float("nan")):
                with pytest.raises(ValueError):
                    validate_risk_settings({field: bad})

    def test_sport_filter_allows_only_known_leagues(self):
        assert normalize_sport_filter("mlb, nfl,MLB") == "MLB,NFL"
        assert normalize_sport_filter("") is None and normalize_sport_filter(None) is None
        for bad in ("MLB; DROP TABLE x", "<script>", "NBA", "x" * 100, 5):
            with pytest.raises(ValueError):
                normalize_sport_filter(bad)


class TestCredentialShape:
    def test_reasonable_values_pass(self):
        assert validate_credential_inputs("a1b2c3d4-e5f6-7890-abcd-ef1234567890", "-----BEGIN PRIVATE KEY-----\nabc") is None

    @pytest.mark.parametrize("key_id,priv", [
        ("", "x"), ("x", ""), (None, "x"), ("x", None), ("k" * 500, "x"), ("bad key id!", "x"),
        ("<script>alert(1)</script>", "x"), ("k'; DROP TABLE t; --", "x"), ("k", "p" * 20000), ("k", "a\x00b"),
    ])
    def test_hostile_or_malformed_credentials_rejected_without_echoing(self, key_id, priv):
        message = validate_credential_inputs(key_id, priv)
        assert message
        assert "DROP" not in message and "script" not in message


def _pem():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def _b64():
    raw = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return base64.b64encode(raw).decode()


@pytest.mark.parametrize("module,provider_cls,priv", [(ck, ck.KalshiProvider, _pem), (cp, cp.PolymarketUSProvider, _b64)])
class TestPlatformModulesEnforceIt:
    def _connected(self, db_conn, module, provider_cls, priv):
        acct = sign_up(db_conn, "u@example.com", None, "correct-horse-1", False)
        with mock.patch.object(provider_cls, "health_check", return_value=mock.MagicMock(ok=True)):
            ok, msg = module.connect_account(db_conn, acct.account_id, "key-1", priv())
        assert ok, msg
        return acct

    def test_save_risk_settings_rejects_hostile_values_and_stores_nothing(self, db_conn, module, provider_cls, priv):
        acct = self._connected(db_conn, module, provider_cls, priv)
        before = module.get_risk_settings(db_conn, acct.account_id)
        for bad in ({"max_bet_usd": float("nan")}, {"unit_size_usd": -5}, {"max_total_exposure_usd": 1e12},
                    {"sport_filter": "'; DROP TABLE x;--"}, {"min_net_ev_pct": float("inf")}):
            with pytest.raises(ValueError):
                module.save_risk_settings(db_conn, acct.account_id, **bad)
        assert module.get_risk_settings(db_conn, acct.account_id) == before

    def test_sport_filter_is_normalized_on_save(self, db_conn, module, provider_cls, priv):
        acct = self._connected(db_conn, module, provider_cls, priv)
        module.save_risk_settings(db_conn, acct.account_id, sport_filter="mlb,nfl")
        assert module.get_risk_settings(db_conn, acct.account_id)["sport_filter"] == "MLB,NFL"

    def test_connect_rejects_malformed_input_before_any_provider_call(self, db_conn, module, provider_cls, priv):
        acct = sign_up(db_conn, "v@example.com", None, "correct-horse-1", False)
        with mock.patch.object(provider_cls, "health_check") as health:
            ok, msg = module.connect_account(db_conn, acct.account_id, "bad key id!", "x")
        assert ok is False and msg
        health.assert_not_called()

    def test_credential_verification_attempts_are_rate_limited(self, db_conn, module, provider_cls, priv):
        acct = sign_up(db_conn, "w@example.com", None, "correct-horse-1", False)
        with mock.patch.object(provider_cls, "health_check", return_value=mock.MagicMock(ok=False)) as health:
            results = [module.connect_account(db_conn, acct.account_id, "key-1", priv()) for _ in range(8)]
        assert health.call_count == 5                                  # attempts 6-8 never reached the provider
        assert all(ok is False for ok, _ in results)
        assert "Too many" in results[-1][1]

    def test_rate_limit_is_per_account(self, db_conn, module, provider_cls, priv):
        a = sign_up(db_conn, "a@example.com", None, "correct-horse-1", False)
        b = sign_up(db_conn, "b@example.com", None, "correct-horse-1", False)
        with mock.patch.object(provider_cls, "health_check", return_value=mock.MagicMock(ok=False)) as health:
            for _ in range(6):
                module.connect_account(db_conn, a.account_id, "key-1", priv())
            module.connect_account(db_conn, b.account_id, "key-1", priv())
        assert health.call_count == 6                                  # 5 for A, 1 for B


class TestAccountSettingsValidation:
    def test_unit_and_state_are_validated_server_side(self, db_conn):
        from src.customer_accounts import get_settings, save_settings
        acct = sign_up(db_conn, "s@example.com", None, "correct-horse-1", False)
        for bad_unit in (float("nan"), float("inf"), -5, 0, 5000, True, "10"):
            with pytest.raises(ValueError):
                save_settings(db_conn, acct.account_id, bad_unit, "NJ")
        for bad_state in ("New Jersey", "N1", "'; DROP TABLE x;--", 5, ""):
            with pytest.raises(ValueError):
                save_settings(db_conn, acct.account_id, 10.0, bad_state)
        assert get_settings(db_conn, acct.account_id) == {"unit_usd": None, "state": None}
        save_settings(db_conn, acct.account_id, 25.0, "nj")
        assert get_settings(db_conn, acct.account_id) == {"unit_usd": 25.0, "state": "NJ"}
        save_settings(db_conn, acct.account_id, None, None)


class TestApprovalRateLimit:
    def test_approval_clicks_are_bounded_per_account(self, db_conn):
        from src.execution.customer_autobet import approve_pending_order
        acct = sign_up(db_conn, "r@example.com", None, "correct-horse-1", False)
        results = [approve_pending_order(db_conn, mock.MagicMock(), acct.account_id, "kalshi", 424242) for _ in range(35)]
        assert all(ok is False for ok, _ in results)
        assert sum("Too many" in msg for _ok, msg in results) == 5      # attempts 31-35 blocked before any lookup
