"""Launch-audit hardening of customer authentication (src/customer_accounts.py):
login rate limiting, disabled accounts, timing equalization, password bounds,
signup race, token expiry, resend cooldown, session hygiene, password reset."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import src.customer_accounts as ca
from src.customer_accounts import (
    RateLimitError, SignUpError, create_session, get_account_by_session, log_in, request_email_verification,
    request_password_reset, reset_password, set_account_disabled, sign_up, verify_email_token,
)


def _acct(conn, email="user@example.com", password="correct-horse-1"):
    return sign_up(conn, email, None, password, False)


class TestLoginRateLimit:
    def test_five_failures_lock_that_email_even_for_the_right_password(self, db_conn):
        _acct(db_conn)
        for _ in range(ca.LOGIN_MAX_FAILURES):
            assert log_in(db_conn, "user@example.com", "wrong-password") is None
        with pytest.raises(RateLimitError):
            log_in(db_conn, "user@example.com", "correct-horse-1")

    def test_lockout_is_per_email(self, db_conn):
        _acct(db_conn)
        _acct(db_conn, "other@example.com")
        for _ in range(ca.LOGIN_MAX_FAILURES):
            log_in(db_conn, "user@example.com", "bad")
        assert log_in(db_conn, "other@example.com", "correct-horse-1") is not None

    def test_success_clears_the_failure_count(self, db_conn):
        _acct(db_conn)
        for _ in range(ca.LOGIN_MAX_FAILURES - 1):
            log_in(db_conn, "user@example.com", "bad")
        assert log_in(db_conn, "user@example.com", "correct-horse-1") is not None
        for _ in range(ca.LOGIN_MAX_FAILURES - 1):
            assert log_in(db_conn, "user@example.com", "bad") is None     # not locked: counter restarted

    def test_failures_outside_the_window_do_not_count(self, db_conn):
        _acct(db_conn)
        old = (datetime.now(timezone.utc) - timedelta(seconds=ca.LOGIN_WINDOW_SECONDS + 60)).isoformat()
        key = ca._rate_key("user@example.com")
        for i in range(10):
            db_conn.execute("INSERT INTO auth_rate_events VALUES (?, 'login_fail', ?, ?)", (f"e{i}", key, old))
        db_conn.commit()
        assert log_in(db_conn, "user@example.com", "correct-horse-1") is not None

    def test_the_event_log_never_stores_the_email(self, db_conn):
        _acct(db_conn)
        log_in(db_conn, "user@example.com", "bad")
        rows = db_conn.execute("SELECT * FROM auth_rate_events").fetchall()
        assert rows and all("user@example.com" not in str(dict(r)) for r in rows)


class TestIdentityAndEnumeration:
    def test_case_variants_resolve_to_one_account(self, db_conn):
        _acct(db_conn, "Test@Email.com")
        assert log_in(db_conn, "test@email.com", "correct-horse-1") is not None
        assert log_in(db_conn, "TEST@EMAIL.COM", "correct-horse-1") is not None
        with pytest.raises(SignUpError):
            _acct(db_conn, "test@EMAIL.com")

    def test_unknown_email_still_performs_a_bcrypt_comparison(self, db_conn):
        with mock.patch.object(ca.bcrypt, "checkpw", return_value=False) as checkpw:
            assert log_in(db_conn, "nobody@example.com", "whatever-pw") is None
        checkpw.assert_called_once()

    def test_wrong_password_unknown_email_and_disabled_are_indistinguishable(self, db_conn):
        a = _acct(db_conn)
        set_account_disabled(db_conn, a.account_id, True)
        assert log_in(db_conn, "user@example.com", "correct-horse-1") is None
        assert log_in(db_conn, "user@example.com", "wrong") is None
        assert log_in(db_conn, "nobody@example.com", "wrong") is None

    def test_invalid_emails_rejected(self, db_conn):
        for bad in ("", "no-at-sign", "a@b", "a b@c.com", "x" * 260 + "@e.com"):
            with pytest.raises(SignUpError):
                sign_up(db_conn, bad, None, "correct-horse-1", False)


class TestPasswordBounds:
    def test_too_long_password_is_rejected_not_truncated(self, db_conn):
        with pytest.raises(SignUpError):
            sign_up(db_conn, "a@example.com", None, "x" * 73, False)

    def test_72_byte_and_unicode_passwords_work(self, db_conn):
        sign_up(db_conn, "a@example.com", None, "x" * 72, False)
        assert log_in(db_conn, "a@example.com", "x" * 72) is not None
        sign_up(db_conn, "b@example.com", None, "pässwörd-ünï", False)
        assert log_in(db_conn, "b@example.com", "pässwörd-ünï") is not None

    def test_login_with_oversized_or_non_string_password_fails_cleanly(self, db_conn):
        _acct(db_conn)
        assert log_in(db_conn, "user@example.com", "y" * 5000) is None
        assert log_in(db_conn, "user@example.com", None) is None


class TestSignupProtection:
    def test_site_wide_hourly_signup_ceiling(self, db_conn):
        for i in range(ca.SIGNUP_MAX_PER_HOUR):
            sign_up(db_conn, f"u{i}@example.com", None, "correct-horse-1", False)
        with pytest.raises(RateLimitError):
            sign_up(db_conn, "one-more@example.com", None, "correct-horse-1", False)

    def test_concurrent_duplicate_signup_becomes_a_clean_duplicate_error(self, db_conn):
        _acct(db_conn)

        class _RacyConn:
            """Existence pre-check misses the row (the race), the INSERT then
            hits the UNIQUE index."""
            def __init__(self, inner):
                self._inner = inner
                self._skipped = False

            def execute(self, sql, params=()):
                if "SELECT account_id FROM customer_accounts WHERE email" in sql and not self._skipped:
                    self._skipped = True
                    class _Empty:
                        def fetchone(self_inner):
                            return None
                    return _Empty()
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        with pytest.raises(SignUpError, match="already exists"):
            sign_up(_RacyConn(db_conn), "user@example.com", None, "correct-horse-1", False)
        n = db_conn.execute("SELECT COUNT(*) AS n FROM customer_accounts").fetchone()["n"]
        assert n == 1


class TestDisabledAccounts:
    def test_disabling_ends_existing_sessions_and_blocks_new_logins(self, db_conn):
        a = _acct(db_conn)
        token = create_session(db_conn, a.account_id)
        assert get_account_by_session(db_conn, token) is not None
        set_account_disabled(db_conn, a.account_id, True)
        assert get_account_by_session(db_conn, token) is None
        assert log_in(db_conn, "user@example.com", "correct-horse-1") is None
        set_account_disabled(db_conn, a.account_id, False)
        assert log_in(db_conn, "user@example.com", "correct-horse-1") is not None

    def test_a_session_created_before_disable_is_refused_even_without_deletion(self, db_conn):
        a = _acct(db_conn)
        token = create_session(db_conn, a.account_id)
        db_conn.execute("UPDATE customer_accounts SET disabled = 1 WHERE account_id = ?", (a.account_id,))
        db_conn.commit()
        assert get_account_by_session(db_conn, token) is None


class TestSessionHygiene:
    def test_expired_session_is_rejected_and_deleted(self, db_conn):
        a = _acct(db_conn)
        token = create_session(db_conn, a.account_id)
        db_conn.execute("UPDATE customer_sessions SET expires_at = ? WHERE session_token = ?",
                        ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), token))
        db_conn.commit()
        assert get_account_by_session(db_conn, token) is None
        assert db_conn.execute("SELECT COUNT(*) AS n FROM customer_sessions").fetchone()["n"] == 0

    def test_last_seen_is_not_rewritten_on_every_lookup(self, db_conn):
        a = _acct(db_conn)
        token = create_session(db_conn, a.account_id)
        before = db_conn.execute("SELECT last_seen_at FROM customer_sessions").fetchone()["last_seen_at"]
        get_account_by_session(db_conn, token)
        get_account_by_session(db_conn, token)
        after = db_conn.execute("SELECT last_seen_at FROM customer_sessions").fetchone()["last_seen_at"]
        assert before == after

    def test_stale_last_seen_is_refreshed(self, db_conn):
        a = _acct(db_conn)
        token = create_session(db_conn, a.account_id)
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        db_conn.execute("UPDATE customer_sessions SET last_seen_at = ?", (old,))
        db_conn.commit()
        get_account_by_session(db_conn, token)
        assert db_conn.execute("SELECT last_seen_at FROM customer_sessions").fetchone()["last_seen_at"] > old

    def test_tokens_are_unguessable_and_unique(self, db_conn):
        a = _acct(db_conn)
        tokens = {create_session(db_conn, a.account_id) for _ in range(20)}
        assert len(tokens) == 20 and all(len(t) >= 40 for t in tokens)

    def test_a_forged_or_other_users_token_grants_nothing(self, db_conn):
        a = _acct(db_conn)
        create_session(db_conn, a.account_id)
        for forged in ("x", "admin", a.account_id, "' OR '1'='1", "A" * 500):
            assert get_account_by_session(db_conn, forged) is None


class TestEmailVerification:
    def test_token_expires(self, db_conn, monkeypatch):
        monkeypatch.setenv("SENDGRID_API_KEY", "")
        a = _acct(db_conn)
        request_email_verification(db_conn, a)
        token = db_conn.execute("SELECT email_verify_token FROM customer_accounts").fetchone()["email_verify_token"]
        old = (datetime.now(timezone.utc) - timedelta(hours=ca.EMAIL_VERIFY_TTL_HOURS + 1)).isoformat()
        db_conn.execute("UPDATE customer_accounts SET email_verify_sent_at = ?", (old,))
        db_conn.commit()
        assert verify_email_token(db_conn, token) is False
        row = db_conn.execute("SELECT email_verified, email_verify_token FROM customer_accounts").fetchone()
        assert row["email_verified"] == 0 and row["email_verify_token"] is None

    def test_resend_is_rate_limited_per_account(self, db_conn, monkeypatch):
        monkeypatch.setenv("SENDGRID_API_KEY", "k")
        monkeypatch.setenv("SENDGRID_FROM_EMAIL", "f@example.com")
        monkeypatch.setenv("SITE_BASE_URL", "https://site.test")
        a = _acct(db_conn)
        with mock.patch.object(ca.requests, "post", return_value=mock.Mock(status_code=202)) as post:
            assert request_email_verification(db_conn, a) is True
            assert request_email_verification(db_conn, a) is False      # inside the cooldown
        assert post.call_count == 1

    def test_garbage_tokens_are_rejected(self, db_conn):
        for bad in (None, "", 5, "' OR 1=1 --", "x" * 10000):
            assert verify_email_token(db_conn, bad) is False


class TestPasswordReset:
    def _request(self, db_conn, email="user@example.com"):
        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent["body"] = json["content"][0]["value"]
            return mock.Mock(status_code=202)

        with mock.patch.object(ca.requests, "post", side_effect=fake_post), \
             mock.patch.dict("os.environ", {"SENDGRID_API_KEY": "k", "SENDGRID_FROM_EMAIL": "f@x.com",
                                            "SITE_BASE_URL": "https://site.test"}):
            assert request_password_reset(db_conn, email) is None
        body = sent.get("body", "")
        return body.split("?reset=")[1].split()[0] if "?reset=" in body else None

    def test_full_reset_flow_is_single_use_and_kills_sessions(self, db_conn):
        a = _acct(db_conn)
        session = create_session(db_conn, a.account_id)
        token = self._request(db_conn)
        assert token
        assert reset_password(db_conn, token, "brand-new-pass-9") is True
        assert log_in(db_conn, "user@example.com", "brand-new-pass-9") is not None
        assert log_in(db_conn, "user@example.com", "correct-horse-1") is None
        assert get_account_by_session(db_conn, session) is None          # old sessions ended
        assert reset_password(db_conn, token, "another-pass-99") is False  # single use

    def test_only_a_hash_of_the_token_is_stored(self, db_conn):
        _acct(db_conn)
        token = self._request(db_conn)
        stored = str(dict(db_conn.execute("SELECT * FROM customer_accounts").fetchone()))
        assert token not in stored

    def test_unknown_email_gets_the_same_silent_result_and_sends_nothing(self, db_conn):
        with mock.patch.object(ca.requests, "post") as post:
            assert request_password_reset(db_conn, "nobody@example.com") is None
        post.assert_not_called()

    def test_disabled_account_gets_no_reset(self, db_conn):
        a = _acct(db_conn)
        set_account_disabled(db_conn, a.account_id, True)
        assert self._request(db_conn) is None

    def test_expired_token_is_rejected(self, db_conn):
        _acct(db_conn)
        token = self._request(db_conn)
        db_conn.execute("UPDATE customer_accounts SET password_reset_expires_at = ?",
                        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
        db_conn.commit()
        assert reset_password(db_conn, token, "brand-new-pass-9") is False

    def test_weak_new_password_is_rejected_and_token_stays_valid(self, db_conn):
        _acct(db_conn)
        token = self._request(db_conn)
        with pytest.raises(SignUpError):
            reset_password(db_conn, token, "short")
        assert reset_password(db_conn, token, "long-enough-pass-1") is True

    def test_requests_are_rate_limited_per_email(self, db_conn):
        _acct(db_conn)
        with mock.patch.object(ca.requests, "post", return_value=mock.Mock(status_code=202)) as post, \
             mock.patch.dict("os.environ", {"SENDGRID_API_KEY": "k", "SENDGRID_FROM_EMAIL": "f@x.com",
                                            "SITE_BASE_URL": "https://site.test"}):
            for _ in range(ca.RESET_MAX_PER_HOUR + 3):
                request_password_reset(db_conn, "user@example.com")
        assert post.call_count == ca.RESET_MAX_PER_HOUR

    def test_garbage_tokens_never_reset_anything(self, db_conn):
        _acct(db_conn)
        for bad in (None, "", "x", "' OR 1=1 --", 5):
            assert reset_password(db_conn, bad, "brand-new-pass-9") is False
