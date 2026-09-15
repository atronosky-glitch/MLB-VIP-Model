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

import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import requests

logger = logging.getLogger(__name__)

SESSION_TTL_DAYS = 30
MIN_PASSWORD_LENGTH = 8

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
    if not _EMAIL_RE.match(email):
        raise SignUpError("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise SignUpError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

    phone = (phone or "").strip()
    if phone and _validate_phone(phone) is None:
        raise SignUpError("Enter a valid phone number.")

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

    conn.execute(
        """INSERT INTO customer_accounts
               (account_id, email, phone, password_hash, marketing_consent,
                marketing_consent_at, marketing_consent_version)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (account_id, email, phone or None, password_hash, int(marketing_consent),
         consent_at, consent_version),
    )
    conn.commit()

    return Account(
        account_id=account_id, email=email, phone=phone or None,
        email_verified=False, phone_verified=False, marketing_consent=marketing_consent,
    )


def log_in(conn, email: str, password: str) -> Account | None:
    """Returns the Account on success, None on any failure -- wrong
    password and nonexistent email return the exact same None, so a
    caller can never distinguish "no such account" from "wrong
    password" (never reveal whether an email is registered)."""
    email = (email or "").strip().lower()
    row = conn.execute(
        """SELECT account_id, email, phone, password_hash, email_verified,
                  phone_verified, marketing_consent
           FROM customer_accounts WHERE email = ?""",
        (email,),
    ).fetchone()
    if not row:
        return None
    row = dict(row)
    if not bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        return None
    return _row_to_account(row)


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
    """None for a missing, expired, or otherwise invalid token -- never
    raises. Bumps last_seen_at on every successful lookup."""
    if not session_token:
        return None
    row = conn.execute(
        """SELECT s.expires_at, a.account_id, a.email, a.phone, a.email_verified,
                  a.phone_verified, a.marketing_consent
           FROM customer_sessions s
           JOIN customer_accounts a ON a.account_id = s.account_id
           WHERE s.session_token = ?""",
        (session_token,),
    ).fetchone()
    if not row:
        return None
    row = dict(row)
    expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        return None
    conn.execute(
        "UPDATE customer_sessions SET last_seen_at = ? WHERE session_token = ?",
        (datetime.now(timezone.utc).isoformat(), session_token),
    )
    conn.commit()
    return _row_to_account(row)


def delete_session(conn, session_token: str) -> None:
    if not session_token:
        return
    conn.execute("DELETE FROM customer_sessions WHERE session_token = ?", (session_token,))
    conn.commit()


def send_verification_email(account: Account, verify_token: str, base_url: str | None = None) -> bool:
    """Sends a real verification email via SendGrid's v3 REST API.
    Returns True only on a confirmed SendGrid acceptance (2xx). Never
    raises for a missing/invalid API key or a network failure -- logs a
    clear operator-facing line and returns False instead, since a
    verification-email hiccup must never block the customer from using
    the account they just created."""
    api_key = os.environ.get("SENDGRID_API_KEY", "")
    from_email = os.environ.get("SENDGRID_FROM_EMAIL", "")
    if not api_key or not from_email:
        logger.warning(
            "USER ACTION REQUIRED: SENDGRID_API_KEY/SENDGRID_FROM_EMAIL not configured -- "
            "cannot send verification email for account_id=%s", account.account_id,
        )
        return False

    site_base_url = base_url or os.environ.get("SITE_BASE_URL", "")
    verify_link = f"{site_base_url}/?verify={verify_token}" if site_base_url else None
    if not verify_link:
        logger.warning(
            "USER ACTION REQUIRED: SITE_BASE_URL not configured -- cannot build a "
            "verification link for account_id=%s", account.account_id,
        )
        return False

    payload = {
        "personalizations": [{"to": [{"email": account.email}]}],
        "from": {"email": from_email},
        "subject": "Verify your email",
        "content": [{
            "type": "text/plain",
            "value": f"Click to verify your email: {verify_link}\n\nIf you didn't create this account, ignore this email.",
        }],
    }
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload, timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Verification email send failed for account_id=%s: %s", account.account_id, type(exc).__name__)
        return False
    if not (200 <= resp.status_code < 300):
        logger.warning(
            "Verification email rejected by SendGrid for account_id=%s: HTTP %s",
            account.account_id, resp.status_code,
        )
        return False
    return True


def request_email_verification(conn, account: Account, base_url: str | None = None) -> bool:
    """Generates a fresh single-use verification token, stores it, and
    sends the email. Returns whether the send succeeded (see
    send_verification_email) -- the token is stored either way, so a
    retry doesn't need a new account."""
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
    (cleared after success, so it can't be replayed). Returns False for
    a missing/already-used token -- never raises."""
    if not token:
        return False
    row = conn.execute(
        "SELECT account_id FROM customer_accounts WHERE email_verify_token = ?", (token,)
    ).fetchone()
    if not row:
        return False
    account_id = dict(row)["account_id"]
    conn.execute(
        """UPDATE customer_accounts
           SET email_verified = 1, email_verify_token = NULL, updated_at = ?
           WHERE account_id = ?""",
        (datetime.now(timezone.utc).isoformat(), account_id),
    )
    conn.commit()
    return True


def get_settings(conn, account_id: str) -> dict:
    row = conn.execute(
        "SELECT unit_usd, state FROM customer_settings WHERE account_id = ?", (account_id,)
    ).fetchone()
    if not row:
        return {"unit_usd": None, "state": None}
    row = dict(row)
    return {"unit_usd": row.get("unit_usd"), "state": row.get("state")}


def save_settings(conn, account_id: str, unit_usd: float | None, state: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO customer_settings (account_id, unit_usd, state, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (account_id) DO UPDATE SET
               unit_usd = excluded.unit_usd, state = excluded.state, updated_at = excluded.updated_at""",
        (account_id, unit_usd, state, now),
    )
    conn.commit()
