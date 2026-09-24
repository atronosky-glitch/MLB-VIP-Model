"""User A must not reach user B's data through ANY customer-facing function,
and the customer site must never take an identity from the browser (URL /
query params / form fields) -- only from a server-side session."""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

import src.customer_kalshi as ck
import src.customer_polymarket as cp
from database.db_manager import get_autobet_executions, save_autobet_execution
from src.credential_encryption import ENCRYPTION_KEY_ENV_VAR, generate_encryption_key
from src.customer_accounts import (
    create_session, get_account_by_session, get_settings, save_settings, sign_up,
)
from src.customer_performance import get_customer_performance


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv(ENCRYPTION_KEY_ENV_VAR, generate_encryption_key())


def _b64_key():
    raw = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return base64.b64encode(raw).decode()


def _pem():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


@pytest.fixture()
def two_users(db_conn):
    a = sign_up(db_conn, "a@example.com", "5551234567", "correct-horse-1", False)
    b = sign_up(db_conn, "b@example.com", "5559876543", "correct-horse-2", False)
    with mock.patch.object(ck.KalshiProvider, "health_check", return_value=mock.MagicMock(ok=True)):
        ck.connect_account(db_conn, b.account_id, "B-KALSHI-KEY-ID", _pem())
    with mock.patch.object(cp.PolymarketUSProvider, "health_check", return_value=mock.MagicMock(ok=True)):
        cp.connect_account(db_conn, b.account_id, "B-POLY-KEY-ID", _b64_key())
    save_settings(db_conn, b.account_id, 25.0, "NJ")
    save_autobet_execution(db_conn, {
        "execution_id": str(uuid.uuid4()), "account_id": b.account_id, "recommendation_id": "rec-B",
        "matchup": "SECRET-B-MATCHUP", "side": "YES", "stake_usd": 10.0, "requested_quantity": 20.0,
        "filled_quantity": 20.0, "avg_fill_price": 0.5, "status": "EXECUTED", "mode": "LIVE",
        "platform": "kalshi", "provider_order_id": "B-ORDER-ID-999",
    })
    db_conn.execute("INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
                    "VALUES ('s1', 'rec-B', 'WIN', '2026-09-20T00:00:00+00:00')")
    db_conn.commit()
    return a, b


class TestUserACannotSeeUserB:
    def test_settings(self, db_conn, two_users):
        a, _b = two_users
        assert get_settings(db_conn, a.account_id) == {"unit_usd": None, "state": None}

    def test_platform_connection_status_and_credentials(self, db_conn, two_users):
        a, b = two_users
        for module in (ck, cp):
            assert module.get_status(db_conn, a.account_id).connected is False
            assert module.get_decrypted_credentials(db_conn, a.account_id) is None
            assert module.get_risk_settings(db_conn, a.account_id) != module.get_risk_settings(db_conn, b.account_id) \
                or module.get_status(db_conn, a.account_id).connected is False

    def test_status_objects_never_carry_secrets(self, db_conn, two_users):
        _a, b = two_users
        for module, secret in ((ck, "B-KALSHI-KEY-ID"), (cp, "B-POLY-KEY-ID")):
            status = module.get_status(db_conn, b.account_id)
            assert secret not in repr(status) and "PRIVATE KEY" not in repr(status)

    def test_stored_credentials_are_encrypted_at_rest(self, db_conn, two_users):
        _a, b = two_users
        for table, secret in (("customer_kalshi_accounts", "B-KALSHI-KEY-ID"),
                              ("customer_polymarket_accounts", "B-POLY-KEY-ID")):
            row = str(dict(db_conn.execute(f"SELECT * FROM {table} WHERE account_id = ?", (b.account_id,)).fetchone()))
            assert secret not in row and "BEGIN PRIVATE KEY" not in row

    def test_execution_history_order_ids_and_activity(self, db_conn, two_users):
        a, _b = two_users
        for platform in (None, "kalshi", "polymarket_us"):
            assert get_autobet_executions(db_conn, a.account_id, limit=100, platform=platform) == []

    def test_my_performance(self, db_conn, two_users):
        a, b = two_users
        pa = get_customer_performance(db_conn, a.account_id, mode="LIVE")
        pb = get_customer_performance(db_conn, b.account_id, mode="LIVE")
        assert pa["total_bets"] == 0 and pa["realized_pnl_usd"] == 0
        assert pb["total_bets"] == 1 and pb["realized_pnl_usd"] == pytest.approx(10.0)

    def test_pending_approval_queue(self, db_conn, two_users):
        from src.execution.live import customer_store
        a, _b = two_users
        assert customer_store.get_ready_prepared_orders_for_account(db_conn, a.account_id) == []

    def test_a_session_resolves_only_to_its_own_account(self, db_conn, two_users):
        a, b = two_users
        ta, tb = create_session(db_conn, a.account_id), create_session(db_conn, b.account_id)
        assert get_account_by_session(db_conn, ta).email == "a@example.com"
        assert get_account_by_session(db_conn, tb).email == "b@example.com"

    def test_user_a_cannot_approve_or_reject_user_bs_pending_order(self, db_conn, two_users):
        # ownership is enforced inside approve/reject (covered in depth in
        # tests/test_customer_autobet.py::TestManualApprovalQueue); assert here
        # that an unknown / foreign id is refused without side effects.
        from src.execution.customer_autobet import approve_pending_order, reject_pending_order
        a, _b = two_users
        ok, _msg = approve_pending_order(db_conn, mock.MagicMock(), a.account_id, "kalshi", 999999)
        assert ok is False
        assert reject_pending_order(db_conn, a.account_id, "kalshi", 999999) is False


class TestBrowserSuppliedIdentityIsNeverTrusted:
    SRC = Path("src/customer_view.py").read_text(encoding="utf-8")

    def test_only_known_non_identity_query_params_are_read(self):
        params = set(re.findall(r'st\.query_params\.(?:get|pop)\(\s*"([^"]+)"', self.SRC))
        assert params <= {"access", "verify", "reset", "page"}
        assert not any(p in params for p in ("account", "account_id", "user", "user_id", "email"))

    def test_account_identity_only_comes_from_the_server_side_session(self):
        # every account_id used by the account-scoped renderers derives from
        # the authenticated Account object or a value stored from it
        assert "account_id = st.query_params" not in self.SRC
        assert 'st.text_input("Account' not in self.SRC and 'st.text_input("User' not in self.SRC
        assert "_current_account_id" in self.SRC
        assert "current_account = _current_account()" in self.SRC

    def test_forged_session_state_id_cannot_outlive_a_missing_session(self):
        # _save_bet_now_settings_on_change reads _current_account_id, which is
        # (re)written only from a validated session each run; log-out clears it
        assert '"_current_account_id"' in self.SRC.split("def _log_out()")[1].split("def _market_label")[0]
