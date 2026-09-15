"""Tests for src/customer_accounts.py -- real per-customer identity for
the customer-facing site (email+password login, phone collected
unverified, sessions, per-account settings, SendGrid email
verification). Hard requirement under test throughout: nothing here
ever logs or exposes a raw password or password hash."""

import logging
from unittest import mock

import pytest

from src.customer_accounts import (
    Account, SignUpError, sign_up, log_in, create_session, get_account_by_session,
    delete_session, send_verification_email, request_email_verification,
    verify_email_token, get_settings, save_settings, MARKETING_CONSENT_VERSION,
)


class TestSignUp:
    def test_creates_an_account(self, db_conn):
        account = sign_up(db_conn, "test@example.com", "555-123-4567", "hunter22", True)
        assert account.email == "test@example.com"
        assert account.phone == "555-123-4567"
        assert account.email_verified is False
        assert account.phone_verified is False
        assert account.marketing_consent is True

    def test_email_lowercased_and_stripped(self, db_conn):
        account = sign_up(db_conn, "  Test@Example.COM  ", None, "hunter22", False)
        assert account.email == "test@example.com"

    def test_malformed_email_rejected(self, db_conn):
        with pytest.raises(SignUpError):
            sign_up(db_conn, "not-an-email", None, "hunter22", False)

    def test_too_short_password_rejected(self, db_conn):
        with pytest.raises(SignUpError):
            sign_up(db_conn, "test@example.com", None, "short", False)

    def test_malformed_phone_rejected(self, db_conn):
        with pytest.raises(SignUpError):
            sign_up(db_conn, "test@example.com", "not a phone", "hunter22", False)

    def test_phone_is_optional(self, db_conn):
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        assert account.phone is None
        account2 = sign_up(db_conn, "test2@example.com", "", "hunter22", False)
        assert account2.phone is None

    def test_duplicate_email_rejected(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22", False)
        with pytest.raises(SignUpError):
            sign_up(db_conn, "test@example.com", None, "differentpw", False)

    def test_duplicate_email_case_insensitive(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22", False)
        with pytest.raises(SignUpError):
            sign_up(db_conn, "TEST@EXAMPLE.COM", None, "differentpw", False)

    def test_no_consent_leaves_consent_fields_null(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22", False)
        row = dict(db_conn.execute(
            "SELECT marketing_consent, marketing_consent_at, marketing_consent_version "
            "FROM customer_accounts WHERE email='test@example.com'"
        ).fetchone())
        assert row["marketing_consent"] == 0
        assert row["marketing_consent_at"] is None
        assert row["marketing_consent_version"] is None

    def test_consent_checked_records_timestamp_and_version(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22", True)
        row = dict(db_conn.execute(
            "SELECT marketing_consent, marketing_consent_at, marketing_consent_version "
            "FROM customer_accounts WHERE email='test@example.com'"
        ).fetchone())
        assert row["marketing_consent"] == 1
        assert row["marketing_consent_at"] is not None
        assert row["marketing_consent_version"] == MARKETING_CONSENT_VERSION

    def test_password_never_stored_in_plaintext(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22plaintext", False)
        row = dict(db_conn.execute(
            "SELECT password_hash FROM customer_accounts WHERE email='test@example.com'"
        ).fetchone())
        assert "hunter22plaintext" not in row["password_hash"]
        assert row["password_hash"].startswith("$2b$") or row["password_hash"].startswith("$2a$")

    def test_password_never_logged(self, db_conn, caplog):
        with caplog.at_level(logging.DEBUG):
            sign_up(db_conn, "test@example.com", None, "supersecretpw123", False)
        for record in caplog.records:
            assert "supersecretpw123" not in record.getMessage()


class TestLogIn:
    def test_correct_password_succeeds(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22", False)
        account = log_in(db_conn, "test@example.com", "hunter22")
        assert account is not None
        assert account.email == "test@example.com"

    def test_wrong_password_fails(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22", False)
        assert log_in(db_conn, "test@example.com", "wrongpassword") is None

    def test_nonexistent_email_fails(self, db_conn):
        assert log_in(db_conn, "nobody@example.com", "hunter22") is None

    def test_login_is_case_insensitive_on_email(self, db_conn):
        sign_up(db_conn, "test@example.com", None, "hunter22", False)
        assert log_in(db_conn, "TEST@EXAMPLE.COM", "hunter22") is not None

    def test_password_never_logged_on_login(self, db_conn, caplog):
        sign_up(db_conn, "test@example.com", None, "hunter22", False)
        with caplog.at_level(logging.DEBUG):
            log_in(db_conn, "test@example.com", "hunter22")
            log_in(db_conn, "test@example.com", "wrongpassword")
        for record in caplog.records:
            assert "hunter22" not in record.getMessage()
            assert "wrongpassword" not in record.getMessage()


class TestSessions:
    def test_create_and_look_up_session(self, db_conn):
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        token = create_session(db_conn, account.account_id)
        looked_up = get_account_by_session(db_conn, token)
        assert looked_up is not None
        assert looked_up.account_id == account.account_id

    def test_missing_token_returns_none(self, db_conn):
        assert get_account_by_session(db_conn, "not-a-real-token") is None

    def test_empty_token_returns_none(self, db_conn):
        assert get_account_by_session(db_conn, "") is None
        assert get_account_by_session(db_conn, None) is None

    def test_expired_session_returns_none(self, db_conn):
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        token = create_session(db_conn, account.account_id)
        db_conn.execute(
            "UPDATE customer_sessions SET expires_at = '2020-01-01T00:00:00+00:00' WHERE session_token = ?",
            (token,),
        )
        db_conn.commit()
        assert get_account_by_session(db_conn, token) is None

    def test_delete_session_invalidates_it(self, db_conn):
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        token = create_session(db_conn, account.account_id)
        delete_session(db_conn, token)
        assert get_account_by_session(db_conn, token) is None

    def test_delete_missing_session_does_not_raise(self, db_conn):
        delete_session(db_conn, "not-a-real-token")
        delete_session(db_conn, "")


class TestEmailVerification:
    def test_missing_sendgrid_key_returns_false_not_raise(self, db_conn, monkeypatch):
        monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        assert request_email_verification(db_conn, account, base_url="https://example.com") is False

    def test_missing_site_base_url_returns_false_not_raise(self, db_conn, monkeypatch):
        monkeypatch.setenv("SENDGRID_API_KEY", "fake-key")
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "noreply@example.com")
        monkeypatch.delenv("SITE_BASE_URL", raising=False)
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        assert request_email_verification(db_conn, account, base_url=None) is False

    def test_successful_send_calls_sendgrid_and_returns_true(self, db_conn, monkeypatch):
        monkeypatch.setenv("SENDGRID_API_KEY", "fake-key")
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "noreply@example.com")
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        fake_resp = mock.Mock(status_code=202)
        with mock.patch("src.customer_accounts.requests.post", return_value=fake_resp) as mock_post:
            result = request_email_verification(db_conn, account, base_url="https://example.com")
        assert result is True
        assert mock_post.called
        sent_json = mock_post.call_args.kwargs["json"]
        assert sent_json["personalizations"][0]["to"][0]["email"] == "test@example.com"
        assert "https://example.com/?verify=" in sent_json["content"][0]["value"]

    def test_sendgrid_rejection_returns_false(self, db_conn, monkeypatch):
        monkeypatch.setenv("SENDGRID_API_KEY", "fake-key")
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "noreply@example.com")
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        fake_resp = mock.Mock(status_code=401)
        with mock.patch("src.customer_accounts.requests.post", return_value=fake_resp):
            assert request_email_verification(db_conn, account, base_url="https://example.com") is False

    def test_network_error_returns_false_not_raise(self, db_conn, monkeypatch):
        import requests as requests_module
        monkeypatch.setenv("SENDGRID_API_KEY", "fake-key")
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "noreply@example.com")
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        with mock.patch("src.customer_accounts.requests.post", side_effect=requests_module.ConnectionError("boom")):
            assert request_email_verification(db_conn, account, base_url="https://example.com") is False

    def test_verify_token_marks_account_verified(self, db_conn, monkeypatch):
        monkeypatch.setenv("SENDGRID_API_KEY", "fake-key")
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "noreply@example.com")
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        with mock.patch("src.customer_accounts.requests.post", return_value=mock.Mock(status_code=202)) as mock_post:
            request_email_verification(db_conn, account, base_url="https://example.com")
        sent_link = mock_post.call_args.kwargs["json"]["content"][0]["value"]
        token = sent_link.split("?verify=")[1].split("\n")[0]

        assert verify_email_token(db_conn, token) is True
        row = dict(db_conn.execute(
            "SELECT email_verified, email_verify_token FROM customer_accounts WHERE account_id = ?",
            (account.account_id,),
        ).fetchone())
        assert row["email_verified"] == 1
        assert row["email_verify_token"] is None

    def test_wrong_token_returns_false(self, db_conn):
        assert verify_email_token(db_conn, "not-a-real-token") is False

    def test_token_is_single_use(self, db_conn, monkeypatch):
        monkeypatch.setenv("SENDGRID_API_KEY", "fake-key")
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "noreply@example.com")
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        with mock.patch("src.customer_accounts.requests.post", return_value=mock.Mock(status_code=202)) as mock_post:
            request_email_verification(db_conn, account, base_url="https://example.com")
        sent_link = mock_post.call_args.kwargs["json"]["content"][0]["value"]
        token = sent_link.split("?verify=")[1].split("\n")[0]
        assert verify_email_token(db_conn, token) is True
        assert verify_email_token(db_conn, token) is False


class TestSettings:
    def test_defaults_are_none_before_any_save(self, db_conn):
        settings = get_settings(db_conn, "some-account-id")
        assert settings == {"unit_usd": None, "state": None}

    def test_save_and_load_round_trip(self, db_conn):
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        save_settings(db_conn, account.account_id, 10.0, "PA")
        assert get_settings(db_conn, account.account_id) == {"unit_usd": 10.0, "state": "PA"}

    def test_save_twice_updates_not_duplicates(self, db_conn):
        account = sign_up(db_conn, "test@example.com", None, "hunter22", False)
        save_settings(db_conn, account.account_id, 10.0, "PA")
        save_settings(db_conn, account.account_id, 25.0, "NJ")
        assert get_settings(db_conn, account.account_id) == {"unit_usd": 25.0, "state": "NJ"}
        count = dict(db_conn.execute(
            "SELECT COUNT(*) AS n FROM customer_settings WHERE account_id = ?", (account.account_id,)
        ).fetchone())
        assert count["n"] == 1
