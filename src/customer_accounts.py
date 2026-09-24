"""Customer accounts for the customer-facing site (src/customer_view.py).

Real per-customer identity, replacing the single-shared-secret access
gate: email + password login, phone collected (unverified -- see below)
for the site's own future marketing use, and per-account settings
($/unit, state) that survive across visits.

Hard rule, matching src/execution/credentials.py's own convention:
nothing in this module ever logs, returns, or otherwise exposes a raw
password or password hash in a log line or exception message. Only
bcrypt's own opaque hash ever touches the database.

Session model: a server-side session store (customer_sessions table),
not a stateless/signed token (JWT, itsdangerous). The browser only ever
holds an opaque session_token (via extra_streamlit_components'
CookieManager, wired up in src/customer_view.py); logging out is a
single DELETE, with no JWT-blocklist problem.

Phone verification is NOT implemented here -- confirmed 2026-09-15 that
real SMS verification (e.g. Twilio Verify) needs a paid third-party
account only the site operator can create, and building toward it would
block this whole feature on that signup. phone_verified stays
permanently 0 for every account for now; the column exists so
verification can be turned on later without a schema change. Phone
numbers are collected and stored exactly as typed, with only a light
format sanity check -- never treated as confirmed-real.

Email verification IS implemented, via SendGrid's v3 Mail Send REST API
(a plain `requests` POST -- no SendGrid SDK dependency needed for one
call). Gated on SENDGRID_API_KEY being configured: a missing key never
blocks account creation, it just leaves the account unverified with a
clear operator-facing log line, matching this codebase's established
"never block on a missing credential" convention (see
src/execution/credentials.py and friends).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import requests

logger = logging.getLogger(__name__)

SESSION_TTL_DAYS = 30
MIN_PASSWORD_LENGTH = 8
# bcrypt only uses the first 72 BYTES of a password (bcrypt>=5 raises on
# more); reject longer input explicitly instead of silently truncating it.
MAX_PASSWORD_BYTES = 72
MAX_EMAIL_LENGTH = 254

# Launch-audit hardening (2026-09-24): DB-backed rate limits so they survive
# restarts and hold across every instance of the customer service.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
SIGNUP_MAX_PER_HOUR = 30                # site-wide ceiling on new accounts
EMAIL_SEND_COOLDOWN_SECONDS = 60        # per-account verification resend spacing
RESET_MAX_PER_HOUR = 3                  # password-reset requests per email
EMAIL_VERIFY_TTL_HOURS = 48
PASSWORD_RESET_TTL_MINUTES = 60
_SESSION_TOUCH_INTERVAL_SECONDS = 300   # bump last_seen_at at most this often

# A real bcrypt hash of a throwaway string: log_in() checks against it when
# the email is unknown so an unknown email and a wrong password take the same
# time (no account-enumeration timing side channel).
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt()).decode("ascii")

# Loose sanity check, not real validation -- catches obvious garbage
# (empty, missing @, no digits at all for phone) without pretending to
# confirm a real, reachable address/number the way actual verification
# would.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_DIGITS_RE = re.compile(r"\d")

# Bumped whenever the consent language below changes, so
# marketing_consent_version on an existing account tells you exactly
# what text that customer agreed to -- required for a real audit trail,
# not just a UI nicety. Kept here (not in customer_view.py) so the
# version always travels with the logic that records it.
MARKETING_CONSENT_VERSION = "v1-2026-09-15"
MARKETING_CONSENT_TEXT = (
    "I agree to receive marketing texts and emails from this site at the "
    "phone number and email I provided. Message and data rates may apply. "
    "Reply STOP to opt out of texts or use the unsubscribe link in any "
    "email. See the Privacy Policy and Terms of Service."
)


@dataclass(frozen=True)
class Account:
    account_id: str
    email: str
    phone: str | None
    email_verified: bool
    phone_verified: bool
    marketing_consent: bool


class SignUpError(ValueError):
    """Raised for a rejected sign-up -- message is always safe to show
    the customer directly (never includes the password)."""


class RateLimitError(ValueError):
    """Too many attempts -- message is safe to show the customer."""


def _rate_key(value: str) -> str:
    """Rate-limit keys are hashed so the event log never stores an email."""
    return hashlib.sha256((value or "").strip().lower().encode("utf-8")).hexdigest()


def _record_event(conn, bucket: str, key: str) -> None:
    conn.execute(
        "INSERT INTO auth_rate_events (event_id, bucket, key, created_at) VALUES (?, ?, ?, ?)",
        (uuid.uuid4().hex, bucket, key, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _count_recent(conn, bucket: str, key: str, window_seconds: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM auth_rate_events WHERE bucket = ? AND key = ? AND created_at >= ?",
        (bucket, key, cutoff),
    ).fetchone()
    return int(dict(row)["n"])


def _clear_events(conn, bucket: str, key: str) -> None:
    conn.execute("DELETE FROM auth_rate_events WHERE bucket = ? AND key = ?", (bucket, key))
    conn.commit()


def allow_rate_limited_action(conn, bucket: str, key: str, max_events: int, window_seconds: int) -> bool:
    """True (and the attempt is recorded) if fewer than *max_events* events of
    this *bucket* for this *key* happened in the window; False otherwise.
    DB-backed, so it holds across restarts and instances. *key* is hashed."""
    hashed = _rate_key(key)
    if _count_recent(conn, bucket, hashed, window_seconds) >= max_events:
        return False
    _record_event(conn, bucket, hashed)
    return True


def purge_old_auth_events(conn, older_than_hours: int = 24) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat()
    conn.execute("DELETE FROM auth_rate_events WHERE created_at < ?", (cutoff,))
    conn.commit()


def _validate_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise SignUpError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise SignUpError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes long.")


def _row_to_account(row: dict) -> Account:
    return Account(
        account_id=row["account_id"],
        email=row["email"],
        phone=row.get("phone"),
        email_verified=bool(row["email_verified"]),
        phone_verified=bool(row["phone_verified"]),
        marketing_consent=bool(row["marketing_consent"]),
    )


def _validate_phone(phone: str) -> str | None:
    """Light format sanity check -- 7 to 15 digits (roughly E.164's own
    bound), ignoring spaces/dashes/parens/a leading +. Returns the
    phone unchanged (never reformats/guesses a canonical form) or None
    if it's obviously not a phone number at all."""
    if not phone:
        return None
    digit_count = len(_PHONE_DIGITS_RE.findall(phone))
    if 7 <= digit_count <= 15:
        return phone
    return None


def sign_up(
    conn, email: str, phone: str | None, password: str, marketing_consent: bool,
) -> Account:
    """Create a new account. Raises SignUpError (safe to show the
    customer) for any rejected input -- never a generic exception that
    might leak internals."""
    email = (email or "").strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        raise SignUpError("Enter a valid email address.")
    _validate_password(password)

    phone = (phone or "").strip()
    if phone and _validate_phone(phone) is None:
        raise SignUpError("Enter a valid phone number.")

    # Site-wide ceiling on new accounts per hour (free site: bots).
    if _count_recent(conn, "signup", "all", 3600) >= SIGNUP_MAX_PER_HOUR:
        raise RateLimitError("Too many sign-ups right now. Please try again later.")

    existing = conn.execute(
        "SELECT account_id FROM customer_accounts WHERE email = ?", (email,)
    ).fetchone()
    if existing:
        raise SignUpError("An account with this email already exists.")

    account_id = secrets.token_urlsafe(16)
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    now = datetime.now(timezone.utc).isoformat()
    consent_at = now if marketing_consent else None
    consent_version = MARKETING_CONSENT_VERSION if marketing_consent else None

    try:
        conn.execute(
            """INSERT INTO customer_accounts
                   (account_id, email, phone, password_hash, marketing_consent,
                    marketing_consent_at, marketing_consent_version)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, email, phone or None, password_hash, int(marketing_consent),
             consent_at, consent_version),
        )
        conn.commit()
    except Exception:
        # Two concurrent sign-ups for the same email: the UNIQUE index rejects
        # the loser -- report it as a duplicate, never a crash.
        try:
            conn.rollback()
        except Exception:
            pass
        dup = conn.execute("SELECT account_id FROM customer_accounts WHERE email = ?", (email,)).fetchone()
        if dup:
            raise SignUpError("An account with this email already exists.")
        raise
    _record_event(conn, "signup", "all")

    return Account(
        account_id=account_id, email=email, phone=phone or None,
        email_verified=False, phone_verified=False, marketing_consent=marketing_consent,
    )


def log_in(conn, email: str, password: str) -> Account | None:
    """Returns the Account on success, None on any failure -- wrong
    password, nonexistent email and a DISABLED account all return the
    exact same None, so a caller can never distinguish them (never reveal
    whether an email is registered). Unknown emails still pay a bcrypt
    check so timing doesn't reveal it either. Raises RateLimitError after
    LOGIN_MAX_FAILURES failures for one email inside the window."""
    email = (email or "").strip().lower()
    password = password if isinstance(password, str) else ""
    key = _rate_key(email)
    if _count_recent(conn, "login_fail", key, LOGIN_WINDOW_SECONDS) >= LOGIN_MAX_FAILURES:
        raise RateLimitError("Too many failed log-in attempts. Please wait 15 minutes and try again.")
    row = conn.execute(
        """SELECT account_id, email, phone, password_hash, email_verified,
                  phone_verified, marketing_consent, disabled
           FROM customer_accounts WHERE email = ?""",
        (email,),
    ).fetchone()
    stored_hash = dict(row)["password_hash"] if row else _DUMMY_HASH
    try:
        password_ok = bcrypt.checkpw(password.encode("utf-8")[:MAX_PASSWORD_BYTES], stored_hash.encode("utf-8"))
    except ValueError:
        password_ok = False
    if not row or not password_ok or dict(row).get("disabled"):
        _record_event(conn, "login_fail", key)
        return None
    _clear_events(conn, "login_fail", key)
    return _row_to_account(dict(row))


def create_session(conn, account_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    conn.execute(
        "INSERT INTO customer_sessions (session_token, account_id, expires_at) VALUES (?, ?, ?)",
        (token, account_id, expires_at),
    )
    conn.commit()
    return token


def get_account_by_session(conn, session_token: str) -> Account | None:
    """None for a missing, expired, disabled-account or otherwise invalid
    token -- never raises. Expired sessions are deleted on sight, and
    last_seen_at is refreshed at most every few minutes (not on every
    Streamlit rerun)."""
    if not session_token:
        return None
    row = conn.execute(
        """SELECT s.expires_at, s.last_seen_at, a.account_id, a.email, a.phone, a.email_verified,
                  a.phone_verified, a.marketing_consent, a.disabled
           FROM customer_sessions s
           JOIN customer_accounts a ON a.account_id = s.account_id
           WHERE s.session_token = ?""",
        (session_token,),
    ).fetchone()
    if not row:
        return None
    row = dict(row)
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        delete_session(conn, session_token)
        return None
    if row.get("disabled"):
        delete_session(conn, session_token)
        return None
    try:
        last_seen = datetime.fromisoformat(str(row.get("last_seen_at") or "").replace("Z", "+00:00"))
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        stale = (now - last_seen).total_seconds() >= _SESSION_TOUCH_INTERVAL_SECONDS
    except ValueError:
        stale = True
    if stale:
        conn.execute(
            "UPDATE customer_sessions SET last_seen_at = ? WHERE session_token = ?",
            (now.isoformat(), session_token),
        )
        conn.commit()
    return _row_to_account(row)


def delete_session(conn, session_token: str) -> None:
    if not session_token:
        return
    conn.execute("DELETE FROM customer_sessions WHERE session_token = ?", (session_token,))
    conn.commit()


def _send_plain_email(to_email: str, subject: str, body: str, log_ref: str) -> bool:
    """Plain-text transactional email via SendGrid v3. True only on a
    confirmed 2xx. Never raises; logs only an opaque reference (never the
    address, token or link)."""
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "")
    if not api_key or not from_email:
        logger.warning(
            "USER ACTION REQUIRED: SENDGRID_API_KEY/SENDGRID_FROM_EMAIL not configured -- "
            "cannot send email (%s)", log_ref,
        )
        return False
    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Email send failed (%s): %s", log_ref, type(exc).__name__)
        return False
    if not (200 <= resp.status_code < 300):
        logger.warning("Email rejected by SendGrid (%s): HTTP %s", log_ref, resp.status_code)
        return False
    return True


def send_verification_email(account: Account, verify_token: str, base_url: str | None = None) -> bool:
    """Sends a real verification email via SendGrid's v3 REST API.
    Returns True only on a confirmed SendGrid acceptance (2xx). Never
    raises for a missing/invalid API key or a network failure -- logs a
    clear operator-facing line and returns False instead, since a
    verification-email hiccup must never block the customer from using
    the account they just created."""
    site_base_url = base_url or os.environ.get("SITE_BASE_URL", "")
    if not (os.environ.get("SENDGRID_API_KEY") and os.environ.get("SENDGRID_FROM_EMAIL")):
        return _send_plain_email(account.email, "", "", f"verify account_id={account.account_id}")
    if not site_base_url:
        logger.warning(
            "USER ACTION REQUIRED: SITE_BASE_URL not configured -- cannot build a "
            "verification link for account_id=%s", account.account_id,
        )
        return False
    verify_link = f"{site_base_url}/?verify={verify_token}"
    return _send_plain_email(
        account.email, "Verify your email",
        f"Click to verify your email: {verify_link}\n\nIf you did not create this account, ignore this email.",
        f"verify account_id={account.account_id}",
    )


def request_email_verification(conn, account: Account, base_url: str | None = None) -> bool:
    """Generates a fresh single-use verification token, stores it, and
    sends the email. Returns whether the send succeeded (see
    send_verification_email) -- the token is stored either way, so a
    retry doesn't need a new account. Resends are spaced by
    EMAIL_SEND_COOLDOWN_SECONDS per account (a free site must not be usable
    as an email cannon)."""
    row = conn.execute(
        "SELECT email_verify_sent_at FROM customer_accounts WHERE account_id = ?", (account.account_id,)
    ).fetchone()
    sent_at = dict(row).get("email_verify_sent_at") if row else None
    if sent_at:
        try:
            last = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last).total_seconds() < EMAIL_SEND_COOLDOWN_SECONDS:
                return False
        except ValueError:
            pass
    token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE customer_accounts SET email_verify_token = ?, email_verify_sent_at = ? WHERE account_id = ?",
        (token, now, account.account_id),
    )
    conn.commit()
    return send_verification_email(account, token, base_url)


def verify_email_token(conn, token: str) -> bool:
    """Marks the matching account verified and single-uses the token
    (cleared after success, so it can't be replayed). Tokens expire after
    EMAIL_VERIFY_TTL_HOURS. Returns False for a missing/used/expired token
    -- never raises."""
    if not token or not isinstance(token, str):
        return False
    row = conn.execute(
        "SELECT account_id, email_verify_sent_at FROM customer_accounts WHERE email_verify_token = ?", (token,)
    ).fetchone()
    if not row:
        return False
    row = dict(row)
    sent_at = row.get("email_verify_sent_at")
    if sent_at:
        try:
            sent = datetime.fromisoformat(str(sent_at).replace("Z", "+00:00"))
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - sent > timedelta(hours=EMAIL_VERIFY_TTL_HOURS):
                conn.execute("UPDATE customer_accounts SET email_verify_token = NULL WHERE account_id = ?",
                             (row["account_id"],))
                conn.commit()
                return False
        except ValueError:
            return False
    conn.execute(
        """UPDATE customer_accounts
           SET email_verified = 1, email_verify_token = NULL, updated_at = ?
           WHERE account_id = ?""",
        (datetime.now(timezone.utc).isoformat(), row["account_id"]),
    )
    conn.commit()
    return True


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def request_password_reset(conn, email: str, base_url: str | None = None) -> None:
    """Start a password reset. ALWAYS returns None -- the caller shows the
    same generic message whether or not the email exists, so this can't be
    used to enumerate accounts. Only a SHA-256 of the single-use token is
    stored; it expires after PASSWORD_RESET_TTL_MINUTES. Limited to
    RESET_MAX_PER_HOUR requests per email."""
    email = (email or "").strip().lower()
    if not email or len(email) > MAX_EMAIL_LENGTH:
        return None
    key = _rate_key(email)
    if _count_recent(conn, "reset_request", key, 3600) >= RESET_MAX_PER_HOUR:
        return None
    _record_event(conn, "reset_request", key)
    row = conn.execute(
        "SELECT account_id, disabled FROM customer_accounts WHERE email = ?", (email,)
    ).fetchone()
    if not row or dict(row).get("disabled"):
        return None
    account_id = dict(row)["account_id"]
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)).isoformat()
    conn.execute(
        "UPDATE customer_accounts SET password_reset_token_hash = ?, password_reset_expires_at = ? WHERE account_id = ?",
        (_hash_token(token), expires, account_id),
    )
    conn.commit()
    site_base_url = base_url or os.environ.get("SITE_BASE_URL", "")
    if not site_base_url:
        logger.warning("USER ACTION REQUIRED: SITE_BASE_URL not configured -- cannot send a password-reset link")
        return None
    _send_plain_email(
        email, "Reset your password",
        f"Use this link to reset your password (valid {PASSWORD_RESET_TTL_MINUTES} minutes, single use):\n"
        f"{site_base_url}/?reset={token}\n\nIf you did not request this, ignore this email.",
        f"reset account_id={account_id}",
    )
    return None


def reset_password(conn, token: str, new_password: str) -> bool:
    """Complete a reset. True on success. The token is single-use and
    expiring; success replaces the password and DELETES every session for
    the account (a stolen session must not survive a reset). Raises
    SignUpError for an unacceptable new password (token stays valid)."""
    if not token or not isinstance(token, str):
        return False
    row = conn.execute(
        "SELECT account_id, password_reset_expires_at FROM customer_accounts WHERE password_reset_token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    if not row:
        return False
    row = dict(row)
    try:
        expires = datetime.fromisoformat(str(row["password_reset_expires_at"]).replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    if expires < datetime.now(timezone.utc):
        return False
    _validate_password(new_password)
    password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")
    conn.execute(
        """UPDATE customer_accounts SET password_hash = ?, password_reset_token_hash = NULL,
               password_reset_expires_at = NULL, updated_at = ? WHERE account_id = ?""",
        (password_hash, datetime.now(timezone.utc).isoformat(), row["account_id"]),
    )
    conn.execute("DELETE FROM customer_sessions WHERE account_id = ?", (row["account_id"],))
    conn.commit()
    return True


def set_account_disabled(conn, account_id: str, disabled: bool) -> None:
    """Operator action: disable/enable an account. Disabling also ends every
    session immediately. (Disabled accounts cannot log in or use a session;
    their Auto-Bet is stopped separately by the operator.)"""
    conn.execute("UPDATE customer_accounts SET disabled = ? WHERE account_id = ?", (1 if disabled else 0, account_id))
    if disabled:
        conn.execute("DELETE FROM customer_sessions WHERE account_id = ?", (account_id,))
    conn.commit()


def get_settings(conn, account_id: str) -> dict:
    row = conn.execute(
        "SELECT unit_usd, state FROM customer_settings WHERE account_id = ?", (account_id,)
    ).fetchone()
    if not row:
        return {"unit_usd": None, "state": None}
    row = dict(row)
    return {"unit_usd": row.get("unit_usd"), "state": row.get("state")}


_US_STATE_RE = re.compile(r"^[A-Za-z]{2}$")


def save_settings(conn, account_id: str, unit_usd: float | None, state: str | None) -> None:
    """Validated: unit_usd must be None or a finite number in [0.5, 1000];
    state must be None or a 2-letter code. Raises ValueError otherwise."""
    if unit_usd is not None:
        import math
        if isinstance(unit_usd, bool) or not isinstance(unit_usd, (int, float)) \
                or math.isnan(unit_usd) or math.isinf(unit_usd) or not (0.5 <= unit_usd <= 1000):
            raise ValueError("unit_usd must be between 0.5 and 1000")
    if state is not None:
        if not isinstance(state, str) or not _US_STATE_RE.match(state.strip()):
            raise ValueError("state must be a 2-letter code")
        state = state.strip().upper()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO customer_settings (account_id, unit_usd, state, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (account_id) DO UPDATE SET
               unit_usd = excluded.unit_usd, state = excluded.state, updated_at = excluded.updated_at""",
        (account_id, unit_usd, state, now),
    )
    conn.commit()
