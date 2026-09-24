"""Admin authentication for the operator dashboard (src/control_panel.py).

Audit finding (2026-09-24, P0): the dashboard service had NO authentication
at all -- anyone who knew its URL could reach scan controls, model overrides
and the live-execution approval panel. This module gates the whole page.

Design:
* The password comes from the ``MLB_ADMIN_PASSWORD`` environment variable
  (a Render secret, ``sync: false``). No password is ever stored in code.
* FAIL CLOSED in production: if ``MLB_ENVIRONMENT=production`` and no
  password is configured, the dashboard refuses to render (rather than
  silently running open). Outside production (local development) an unset
  password allows access with a visible warning.
* Comparison is constant-time (``hmac.compare_digest``).
* Brute-force protection is process-wide, not per browser session (a
  session-scoped counter is defeated by opening a new session): after
  ``MAX_FAILURES`` consecutive failures the gate locks for
  ``LOCKOUT_SECONDS`` for EVERYONE, so guessing is bounded to a handful of
  tries per lockout window regardless of how many sessions an attacker opens.

The decision logic (``AdminGate``) is pure and unit-tested; ``require_admin``
is the thin Streamlit wrapper.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from dataclasses import dataclass, field

ADMIN_PASSWORD_ENV = "MLB_ADMIN_PASSWORD"
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


@dataclass
class AdminGate:
    max_failures: int = MAX_FAILURES
    lockout_seconds: int = LOCKOUT_SECONDS
    _failures: int = 0
    _locked_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def is_locked(self, now: float | None = None) -> float:
        """Seconds remaining on a lockout (0 if not locked)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            return max(0.0, self._locked_until - now)

    def attempt(self, supplied: str, expected: str, now: float | None = None) -> bool:
        """True only for a correct password while not locked out. A locked
        gate rejects even the correct password (so lockout can't be probed)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if now < self._locked_until:
                return False
            ok = bool(expected) and hmac.compare_digest(
                (supplied or "").encode("utf-8"), expected.encode("utf-8"),
            )
            if ok:
                self._failures = 0
                return True
            self._failures += 1
            if self._failures >= self.max_failures:
                self._locked_until = now + self.lockout_seconds
                self._failures = 0
            return False


_GATE = AdminGate()


def configured_password() -> str:
    return os.environ.get(ADMIN_PASSWORD_ENV, "")


def is_production() -> bool:
    return os.environ.get("MLB_ENVIRONMENT", "").strip().lower() == "production"


def access_mode() -> str:
    """"locked_no_password" (production, unset -> refuse), "open_dev"
    (non-production, unset -> allow with warning) or "password"."""
    if configured_password():
        return "password"
    return "locked_no_password" if is_production() else "open_dev"


def require_admin() -> None:
    """Call once at the top of the dashboard script. Returns only for an
    authenticated admin; otherwise renders a login (or a lock message) and
    stops the script."""
    import streamlit as st

    mode = access_mode()
    if mode == "open_dev":
        st.warning(f"Admin password not set ({ADMIN_PASSWORD_ENV}) -- dashboard is OPEN (development only).")
        return
    if mode == "locked_no_password":
        st.error(
            f"Admin dashboard is locked: {ADMIN_PASSWORD_ENV} is not configured on this service. "
            "Set it in the Render dashboard (Environment) and redeploy."
        )
        st.stop()
    if st.session_state.get("_admin_authenticated") is True:
        return

    st.subheader("Admin sign-in")
    remaining = _GATE.is_locked()
    if remaining > 0:
        st.error(f"Too many failed attempts. Try again in {int(remaining) + 1} seconds.")
        st.stop()
    with st.form("admin_login"):
        supplied = st.text_input("Admin password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if _GATE.attempt(supplied, configured_password()):
            st.session_state["_admin_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()
