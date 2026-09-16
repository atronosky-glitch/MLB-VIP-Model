"""Read-only customer-facing VIP product view (MLB, NFL, WNBA).

Public requests never query protected upcoming recommendation fields. The
temporary entitlement adapter uses a server-side staging token so a future
billing provider can replace one function without changing the UI contract.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import altair as alt
import extra_streamlit_components as stx
import pandas as pd
import streamlit as st

from database.db_manager import (
    get_connection, init_db, get_performance_baseline, get_today_in_configured_timezone,
    format_event_start_local, is_event_live, get_bet_links,
)
from src.customer_accounts import (
    Account, SignUpError, sign_up, log_in, create_session, get_account_by_session,
    delete_session, request_email_verification, verify_email_token,
    get_settings as get_account_settings, save_settings as save_account_settings,
    MARKETING_CONSENT_TEXT,
)
from src.grading import performance_summary, breakdown_by_field, assign_bucket, EV_BUCKETS
from src.sportsbook_picker import render_sportsbook_picker
from src.odds_api_client import TRACKED_BOOKMAKERS
from src.tracker import compute_variable_stake

SESSION_COOKIE_NAME = "mlb_vip_session"

logger = logging.getLogger(__name__)


st.set_page_config(page_title="VIP | Sharp Market Intelligence", page_icon="🎯", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&display=swap');
:root {
  --ink:#111827; --muted:#6b7280; --line:#e5e7eb; --panel:#f9fafb;
  --accent:#111827; --accent-soft:#374151; --win:#16a34a; --loss:#dc2626; --ref:#9ca3af;
}
.stApp { background:#ffffff; color:var(--ink); }
[data-testid="stHeader"] { background:rgba(255,255,255,.92); }
h1,h2,h3 { font-family:'Space Grotesk',sans-serif !important; letter-spacing:-.02em; color:var(--ink) !important; }
p,div,span,button { font-family:'Inter',sans-serif; }
.topnav { display:flex; align-items:center; justify-content:space-between; padding:1.1rem 0; border-bottom:1px solid var(--line); flex-wrap:wrap; gap:.9rem; }
.topnav-brand { display:flex; align-items:center; gap:.65rem; }
.topnav-mark { display:inline-flex; align-items:center; justify-content:center; width:2.1rem; height:2.1rem; border-radius:8px; background:var(--accent); color:#fff; font-weight:800; font-family:'Space Grotesk'; font-size:.85rem; }
.topnav-word { color:var(--ink); font-weight:700; font-size:.95rem; letter-spacing:-.01em; }
.topnav-links { display:flex; gap:1.7rem; }
.topnav-links a { color:var(--muted); font-weight:700; font-size:.85rem; text-decoration:none; letter-spacing:.01em; }
.hero { padding:2.4rem 0 1.6rem; }
.eyebrow { color:var(--muted); font-weight:700; letter-spacing:.1em; font-size:.7rem; text-transform:uppercase; }
.hero h1 { font-weight:700 !important; font-size:clamp(2rem,4vw,2.9rem); line-height:1.2; margin:.6rem 0 .9rem; letter-spacing:-.02em !important; }
.hero p { color:var(--muted); font-size:1rem; max-width:680px; line-height:1.6; }
.pill { display:inline-block; padding:.4rem .7rem; border:1px solid var(--line); border-radius:6px; color:var(--muted); font-size:.72rem; font-weight:600; letter-spacing:.03em; }
.pick { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--line); border-radius:8px; padding:1.1rem 1.2rem; margin:.6rem 0; }
.pick.settled { border-left-color:var(--line); }
.pick.win { border-left-color:var(--win); }
.pick.loss { border-left-color:var(--loss); }
.pick.push, .pick.void { border-left-color:var(--ref); }
.pick.locked { border-left-color:var(--accent); }
.pick.research { border-left-color:var(--line); }
.pick-title { font-family:'Space Grotesk'; font-size:1.1rem; font-weight:700; color:var(--ink); }
.pick-meta { color:var(--muted); font-size:.87rem; margin-top:.35rem; }
.edge { color:var(--ink); font-weight:700; }
.result-win { color:var(--win); font-weight:700; }
.result-loss { color:var(--loss); font-weight:700; }
.unit-line { color:var(--ink); font-family:'Space Grotesk'; font-size:.98rem; font-weight:700; margin-top:.5rem; }
.gold { color:var(--ink); font-weight:700; }
.section-note { color:var(--muted); font-size:.9rem; line-height:1.5; }
.lock-copy { color:var(--ink); font-family:'Space Grotesk'; font-weight:600; letter-spacing:.01em; }
.feature { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:1rem; min-height:120px; }
.feature-title { color:var(--muted); font-weight:700; font-size:.8rem; letter-spacing:.05em; text-transform:uppercase; }
.results-panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:1.3rem 1.4rem 1rem; margin:.8rem 0 1.2rem; }
.hero-checklist { margin:.9rem 0 1.4rem; }
.check-item { color:var(--ink); font-size:.96rem; margin:.4rem 0; display:flex; align-items:center; gap:.6rem; }
.check-mark { display:inline-flex; align-items:center; justify-content:center; width:1.25rem; height:1.25rem; border-radius:4px; border:1px solid var(--accent); color:var(--accent); font-size:.7rem; font-weight:800; flex:none; }
.hero-cta { margin:.3rem 0 1.4rem; display:flex; gap:.75rem; flex-wrap:wrap; }
.btn-primary { background:var(--accent); color:#fff; font-weight:700; padding:.68rem 1.3rem; border-radius:6px; text-decoration:none; font-size:.9rem; display:inline-block; }
.bet-now-row { display:flex; justify-content:flex-end; align-items:center; gap:.6rem; margin-top:.75rem; flex-wrap:wrap; }
.bet-now-suggested { color:var(--muted); font-size:.82rem; }
.bet-now-btn { background:var(--win); color:#fff !important; font-weight:800; padding:.55rem 1.1rem; border-radius:6px; text-decoration:none; font-size:.85rem; letter-spacing:.02em; display:inline-block; box-shadow:0 1px 3px rgba(22,163,74,.35); }
.bet-now-btn:hover { background:#15803d; }
.btn-secondary { background:transparent; color:var(--ink); border:1px solid var(--line); font-weight:600; padding:.64rem 1.25rem; border-radius:6px; text-decoration:none; font-size:.9rem; display:inline-block; }
.footer-band { border-top:1px solid var(--line); padding:1.6rem 0 .4rem; margin-top:.6rem; }
.footer-label { color:var(--muted); font-size:.7rem; letter-spacing:.1em; text-transform:uppercase; font-weight:600; }
.footer-books { color:var(--ink); font-size:.92rem; margin-top:.5rem; letter-spacing:0; opacity:.8; }
.results-eyebrow { color:var(--muted); font-weight:700; letter-spacing:.1em; font-size:.68rem; text-transform:uppercase; }
.results-number { font-family:'Space Grotesk',sans-serif; font-size:2.5rem; font-weight:700; line-height:1.05; margin:.3rem 0 .2rem; }
.results-caption { color:var(--muted); font-size:.85rem; max-width:520px; line-height:1.5; }
/* Streamlit's own theme (.streamlit/config.toml) is dark with a bright
   lime primaryColor, shared with the admin dashboard -- this page forces
   its own light theme instead, so every native widget that would
   otherwise pick up the dark-theme defaults (bright lime accents, white
   text meant for a dark background) needs an explicit override here. */
[data-testid="stBaseButton-primary"] {
  background-color:var(--accent) !important; border-color:var(--accent) !important; color:#fff !important;
}
[data-testid="stBaseButton-primary"]:hover {
  background-color:var(--accent-soft) !important; border-color:var(--accent-soft) !important; color:#fff !important;
}
/* Real bug, found live 2026-09-16 (operator screenshot: the Log In/Sign
   Up buttons on the new account-login form were still the shared dark
   theme's bright lime): a button inside st.form_submit_button carries
   its OWN separate testid (stBaseButton-primaryFormSubmit), not the
   plain stBaseButton-primary already overridden above -- same bug
   class as every other one on this page, one more native-widget
   variant the guessed selector never covered. Verified live via the
   browser's own computed styles. */
[data-testid="stBaseButton-primaryFormSubmit"] {
  background-color:var(--accent) !important; border-color:var(--accent) !important; color:#fff !important;
}
[data-testid="stBaseButton-primaryFormSubmit"]:hover {
  background-color:var(--accent-soft) !important; border-color:var(--accent-soft) !important; color:#fff !important;
}
/* Same bug, the account form's text inputs (email/phone/password):
   found live 2026-09-16 (operator screenshot: couldn't read anything
   typed into the Log In/Sign Up fields) -- stTextInputRootElement
   itself carries the shared dark theme's secondaryBackgroundColor
   directly (confirmed live via computed styles: rgb(16,22,33)), with
   near-white text on top of it -- unreadable once this page's own
   light theme is layered on top of the rest of the DOM around it. */
[data-testid="stTextInputRootElement"] {
  background-color:#ffffff !important; border-color:var(--line) !important;
}
[data-testid="stTextInputRootElement"] input { color:var(--ink) !important; }
/* Same bug, the Log In / Sign Up tab selector: found live 2026-09-16
   (operator screenshot) -- the active tab's label text AND its
   underline indicator were both the shared theme's raw lime
   (confirmed live via computed styles: rgb(185,255,69) on both). */
[data-testid="stTab"][data-selected="true"] p { color:var(--accent) !important; }
[data-testid="stTab"] .react-aria-SelectionIndicator {
  background-color:var(--accent) !important; border-color:var(--accent) !important;
}
/* Same bug, the marketing-consent checkbox on the Sign Up form: found
   live 2026-09-16 -- the unchecked box itself carried the shared dark
   theme's near-black background (confirmed live: rgb(13,17,28), a
   barely-visible dark-on-white blob, not a legible unchecked checkbox
   outline -- worth getting right specifically since this is a consent
   control). The visual box has no stable testid of its own (an
   unlabeled div inside the checkbox's <label>); :not([data-testid])
   excludes its sibling stWidgetLabel div, verified live to match only
   the checkbox box itself. */
[data-testid="stCheckbox"] label > div:not([data-testid]) {
  background-color:#ffffff !important; border-color:var(--line) !important;
}
/* input:checked ~ div doesn't match here -- the <input> is nested
   inside a <span> wrapper, not a direct sibling of the visual box, so
   a plain sibling combinator can't reach it (verified live: forcing
   checked=true directly left the box unchanged). :has() reaches from
   the shared <label> ancestor instead -- confirmed live this actually
   flips the box color once the input is checked. */
[data-testid="stCheckbox"] label:has(input:checked) > div:not([data-testid]) {
  background-color:var(--accent) !important; border-color:var(--accent) !important;
}
/* Real bug, found live 2026-09-10 (operator screenshot: "← All Options"
   unreadable on the Middling page): secondary buttons inherit this
   page's dark ink color for their text (from .stApp's own color rule)
   but keep the SHARED dark theme's near-black background -- dark text
   on a near-black button, unreadable. Primary buttons above were fixed
   already; this is the same class of bug for secondary ones. */
[data-testid="stBaseButton-secondary"] {
  background-color:#ffffff !important; border-color:var(--line) !important; color:var(--ink) !important;
}
[data-testid="stBaseButton-secondary"]:hover {
  background-color:var(--panel) !important; border-color:var(--ink) !important; color:var(--ink) !important;
}
/* Same bug, one more spot: found live 2026-09-10 checking the "Filter by
   sportsbook" popover after the button fix above -- the popover TRIGGER
   is its own testid (stPopoverButton, not stBaseButton-secondary), and
   the popover's floating BODY is an entirely separate DOM subtree that
   still used the shared dark theme's background -- readable on its own,
   but visually disconnected from this page's light theme, and the
   stCaptionContainer rule below was leaking a medium-gray color into it
   that read poorly against that dark background. Fixed both. */
[data-testid="stPopoverButton"] {
  background-color:#ffffff !important; border-color:var(--line) !important; color:var(--ink) !important;
}
[data-testid="stPopoverButton"]:hover {
  background-color:var(--panel) !important; border-color:var(--ink) !important; color:var(--ink) !important;
}
[data-testid="stPopoverBody"] { background-color:#ffffff !important; border-color:var(--line) !important; }
[data-testid="stPopoverBody"] [data-testid="stMarkdownContainer"] p { color:var(--ink) !important; }
/* Same bug, the multiselect filters (Sport/Sportsbook/Market/Confidence
   inside "Filter upcoming/settled picks"): found live 2026-09-10 --
   control box was the shared dark theme's near-black, and selected tags
   were the bright lime primaryColor, same as everything above. */
[data-testid="stMultiSelect"] [data-baseweb="select"] > div {
  background-color:#ffffff !important; border-color:var(--line) !important;
}
[data-testid="stMultiSelect"] [data-baseweb="tag"] { background-color:var(--accent) !important; }
[data-testid="stMultiSelect"] input { color:var(--ink) !important; }
[data-baseweb="popover"] [data-baseweb="menu"], [data-baseweb="popover"] [data-baseweb="menu"] * {
  background-color:#ffffff !important; color:var(--ink) !important;
}
[data-testid="stMetricValue"], [data-testid="stMetricLabel"] { color:var(--ink) !important; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * { color:var(--muted) !important; }
[data-testid="stAlertContentInfo"], [data-testid="stAlertContentSuccess"],
[data-testid="stAlertContentWarning"], [data-testid="stAlertContentError"] { color:var(--ink) !important; }
[data-testid="stExpander"] summary { color:var(--ink) !important; }
[data-testid="stSliderThumbValue"], [data-testid="stTickBarMin"], [data-testid="stTickBarMax"] { color:var(--muted) !important; }
/* Real bug, found live 2026-09-10: same story as the radio fix above --
   the guessed data-baseweb="slider"/role="slider" selectors never
   matched this Streamlit version, so the Minimum EV% slider's thumb was
   still bright lime. Verified live: the thumb is the only colored div
   inside stSlider's [role="group"] wrapper, two levels down. */
[data-testid="stSlider"] [role="group"] > div > div { background-color:var(--accent) !important; }
/* Real bug, found live 2026-09-10: the guessed data-baseweb="radio"
   selector below never matched this Streamlit version at all (it uses
   stRadioOption/data-selected, not baseweb) -- the "Performance period"
   radio's selected dot was still rendering in the shared theme's bright
   lime, unfixed. Replaced with the verified real selector (checked live:
   the filled dot is the 3rd nested div inside the selected option). */
[data-testid="stRadioOption"][data-selected="true"] > div > div > div {
  background-color:var(--accent) !important;
}
[data-testid="stSelectbox"] .react-aria-ComboBox > div[role="group"] {
    background-color: #ffffff !important; border: 1px solid var(--line) !important;
}
[data-testid="stSelectbox"] .react-aria-ComboBox input { color: var(--ink) !important; }
[data-testid="stSelectboxVirtualDropdown"], [data-testid="stSelectboxVirtualDropdown"] * {
    background-color: #ffffff !important; color: var(--ink) !important;
}
[data-testid="stSelectboxVirtualDropdown"] [aria-selected="true"] { background-color: var(--panel) !important; }
</style>
""", unsafe_allow_html=True)


def _authorized_request(account: "Account | None" = None) -> bool:
    """Staging entitlement adapter; replace with billing webhook/provider later.

    2026-09-09: full site access opened to everyone (operator decision —
    no paywall while the product is still being validated). To restore
    the token-gated behavior below, set MLB_CUSTOMER_FREE_ACCESS=false
    on the customer-site service — no code change needed either way.

    2026-09-15: a real logged-in account (see src/customer_accounts.py)
    is now an ADDITIONAL way to become authorized, checked only once
    free access is off and the legacy shared token doesn't match —
    neither of those two original paths is touched. In production this
    means requiring a real account is a config flip
    (MLB_CUSTOMER_FREE_ACCESS=false), not a code change: at that point
    the only ways in are the legacy shared token (which the operator can
    simply stop distributing) or signing up for a real account.
    """
    if os.getenv("MLB_CUSTOMER_FREE_ACCESS", "true").strip().lower() != "false":
        return True
    expected = os.getenv("MLB_CUSTOMER_ACCESS_TOKEN", "")
    supplied = st.query_params.get("access", "")
    if expected and supplied and hmac.compare_digest(supplied, expected):
        return True
    return account is not None


def _get_cookie_manager() -> stx.CookieManager:
    """One CookieManager per browser session (st.session_state, not
    @st.cache_resource) -- confirmed live 2026-09-15: this Streamlit
    version's cache-replay-rules policy explicitly forbids a widget-like
    component call (which CookieManager's underlying getAll bidirectional
    component call counts as) inside an @st.cache_resource/@st.cache_data
    function, raising CachedWidgetWarning. session_state avoids that
    entirely and is the correct scope anyway -- one manager per visitor,
    not one shared globally across every visitor on the server."""
    if "_cookie_manager" not in st.session_state:
        st.session_state["_cookie_manager"] = stx.CookieManager(key="mlb_vip_cookie_manager")
    return st.session_state["_cookie_manager"]


def _get_session_token() -> str | None:
    """Reads via st.context.cookies -- an official, public, SYNCHRONOUS
    Streamlit API (added well before this version) that reads the raw
    Cookie request header directly, no custom-component round-trip and
    no race. extra_streamlit_components' CookieManager has no official
    read equivalent this reliable (confirmed live 2026-09-15: a fresh
    CookieManager's own initial getAll() component call doesn't resolve
    on the first script run of a new page load, so .get(...) reliably
    returned None even with a real cookie already sitting in the
    browser) -- it's kept only for SETTING a cookie, since
    st.context.cookies is read-only."""
    return st.context.cookies.get(SESSION_COOKIE_NAME)


def _set_session_cookie(token: str) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    _get_cookie_manager().set(SESSION_COOKIE_NAME, token, expires_at=expires_at, key="set_session_cookie")


def _clear_session_cookie() -> None:
    try:
        _get_cookie_manager().delete(SESSION_COOKIE_NAME, key="delete_session_cookie")
    except KeyError:
        pass  # already absent -- nothing to clear


def _current_account() -> "Account | None":
    """Checks st.session_state first (set immediately after a
    successful login/signup in this same session -- see
    _get_session_token's docstring for why that write-side cookie set
    can't be relied on synchronously within the SAME session), then
    st.context.cookies (the official, synchronous read -- reliable on
    a fresh page load, unlike the CookieManager component read)."""
    token = st.session_state.get("_session_token") or _get_session_token()
    if not token:
        return None
    conn = get_connection()
    try:
        return get_account_by_session(conn, token)
    finally:
        conn.close()


def _log_out() -> None:
    token = st.session_state.get("_session_token") or _get_session_token()
    if token:
        conn = get_connection()
        try:
            delete_session(conn, token)
        finally:
            conn.close()
    _clear_session_cookie()
    # Clear any per-account widget/session state so a different account
    # logging in next (same tab, no reload) never briefly shows the
    # previous account's identity or Bet Now settings before its own
    # DB-loaded defaults land.
    for key in ("bet_now_state", "bet_now_unit_usd", "_current_account_id", "_session_token"):
        st.session_state.pop(key, None)


def _market_label(value: str) -> str:
    return (value or "").replace("_ou", "").replace("_yn", "").replace("_", " ").title()


_LEAGUE_EMOJI = {"MLB": "⚾", "NFL": "🏈", "WNBA": "🏀"}


def _league_badge(pick: dict) -> str:
    league = (pick.get("league") or "MLB").upper()
    return f"{_LEAGUE_EMOJI.get(league, '')} {league}".strip()


# ── "Bet Now" deep links ──────────────────────────────────────────────
#
# Real, live-verified 2026-09-15: The Odds API's includeLinks=true
# returns a per-outcome sportsbook bet-slip deep link (see
# src/odds_api_props_parser.py / src/odds_api_game_parser.py for where
# it's captured, database.db_manager.get_bet_links for the lookup).
# Some books' links carry real template placeholders -- {state}
# (BetMGM, BetRivers -- regulated US betting is per-state) and
# {wagerAmount} (BetRivers only, confirmed live -- a genuine stake
# pre-fill). {pickType} (also BetRivers) could NOT be verified: no
# public docs exist, and this app's browser tool refuses to navigate to
# a real regulated sportsbook URL (a compliance restriction, respected
# here rather than routed around). Any link left with an unresolved
# placeholder after substitution is treated as unusable -- no button
# shown -- rather than risk sending a customer to a malformed bet slip.

_US_STATE_ABBREVIATIONS = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA "
    "WA WV WI WY DC"
).split()

_UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z]+\}")


def _suggested_stake_units(pick: dict) -> float | None:
    """Stake size for the "Bet Now" button's suggested-amount text.

    ``risk_units`` (used by the existing "Stake: X.XXu" result line) is
    only ever populated by src/automatic_grading.py AFTER a pick
    settles -- confirmed live 2026-09-15 it's always None for an
    upcoming/research pick, which would otherwise make a suggested
    dollar amount impossible before the game even happens. Falls back
    to computing a prospective size with the SAME formula
    (src/tracker.py::compute_variable_stake, 25% fractional Kelly) from
    fields already on the pick at recommendation time (ev_pct,
    offered_decimal_odds, model_score) -- not a new sizing concept,
    just the existing one evaluated early instead of retroactively.
    Deliberately does NOT touch the pre-existing "Stake: —" pre-
    settlement display, which may reflect its own separate reason to
    stay blank pre-game."""
    if pick.get("risk_units") is not None:
        return pick["risk_units"]
    return compute_variable_stake(pick.get("ev_pct"), pick.get("offered_decimal_odds"), pick.get("model_score"))


def _resolve_bet_link(raw_link: str | None, state: str | None, stake_usd: float | None) -> tuple[str | None, str | None]:
    """(usable_url, note). usable_url is None when there's no link at
    all, or when a template placeholder in it couldn't be safely
    resolved -- never a guessed or broken URL. note explains why when
    usable_url is None (e.g. "set your state above")."""
    if not raw_link:
        return None, None
    url = raw_link
    if "{state}" in url:
        if not state:
            return None, "Set your state above to enable this link"
        url = url.replace("{state}", state.lower())
    if "{wagerAmount}" in url and stake_usd:
        url = url.replace("{wagerAmount}", f"{stake_usd:.2f}")
    if _UNRESOLVED_PLACEHOLDER_RE.search(url):
        return None, "Deep link unavailable for this book (unverified link format)"
    return url, None


def _render_bet_now_settings(account: "Account | None" = None) -> tuple[str | None, float | None]:
    """State + $-per-unit inputs. Logged in: loaded from and saved to
    customer_settings via _save_bet_now_settings_on_change, so they
    survive across visits. Logged out (only reachable in free-access
    mode): remembered for this browser tab's session only, same as
    before accounts existed -- said so plainly rather than silently
    losing the setting. Returns (state_abbreviation_or_None,
    dollars_per_unit_or_None)."""
    saved_state, saved_unit_usd = None, None
    if account is not None and "bet_now_state" not in st.session_state:
        # Only seed from the DB on the very first render of this widget
        # key in the session -- once the key exists, Streamlit's own
        # session_state governs the displayed value on reruns (passing a
        # different index=/value= after that point has no visible
        # effect), which is what we want: an in-session edit should
        # stick, not be clobbered back to the last-saved DB value.
        saved = get_account_settings(get_connection(), account.account_id)
        saved_state, saved_unit_usd = saved["state"], saved["unit_usd"]

    label = "⚙️ Bet Now settings" if account is not None else "⚙️ Bet Now settings (this visit only)"
    with st.expander(label, expanded=False):
        cols = st.columns(2)
        state_options = ["Not set"] + _US_STATE_ABBREVIATIONS
        with cols[0]:
            state = st.selectbox(
                "Your state", state_options,
                index=state_options.index(saved_state) if saved_state in state_options else 0,
                key="bet_now_state", on_change=_save_bet_now_settings_on_change,
                help="Needed for books whose bet slip link is state-specific (e.g. BetMGM).",
            )
        with cols[1]:
            unit_usd = st.number_input(
                "$ per unit", min_value=0.0,
                value=st.session_state.get("bet_now_unit_usd", saved_unit_usd or 0.0),
                step=1.0, key="bet_now_unit_usd", on_change=_save_bet_now_settings_on_change,
                help="Used to show a suggested dollar stake next to each pick (units × $/unit).",
            )
        if account is not None:
            st.caption("Saved to your account — remembered on your next visit too.")
        else:
            st.caption("Remembered only while this tab stays open — not saved for your next visit.")
    return (None if state == "Not set" else state), (unit_usd or None)


def _bet_now_html(
    label: str, raw_link: str | None, state: str | None,
    stake_units: float | None, unit_usd: float | None,
) -> str:
    """One "Bet Now" button + suggested-stake text, as an HTML snippet
    to embed inside an existing .pick card's markdown block."""
    stake_usd = (stake_units * unit_usd) if (stake_units and unit_usd) else None
    url, note = _resolve_bet_link(raw_link, state, stake_usd)
    if stake_usd is not None:
        suggested = f"Suggested stake: ${stake_usd:.2f}"
    elif stake_units is not None:
        suggested = f"Suggested stake: {stake_units:.2f}u — set $/unit above for a dollar amount"
    else:
        suggested = ""
    if url is None:
        return (
            f'<div class="bet-now-row"><span class="bet-now-suggested">{suggested}</span></div>'
            if suggested else ""
        )
    return (
        f'<div class="bet-now-row"><span class="bet-now-suggested">{suggested}</span>'
        f'<a class="bet-now-btn" href="{url}" target="_blank" rel="noopener">🎯 BET NOW — {label}</a></div>'
    )


def _fair_odds_label(pick: dict) -> str:
    fair = pick.get("fair_american_odds")
    return f"{fair:+d}" if isinstance(fair, int) else ("—" if fair is None else f"{fair:+.0f}")


def _confidence_label(pick: dict) -> str:
    grade = pick.get("confidence_grade")
    score = pick.get("confidence_score")
    if grade and score is not None:
        return f"{grade} ({score:.0f})"
    return grade or "—"


def _settled_status(row: dict) -> str:
    return (row.get("settlement_status") or row.get("outcome") or "").upper()


def _side_line_label(pick: dict) -> str:
    side = (pick.get("side") or "").title()
    market = pick.get("market_type") or ""
    if pick.get("line") is not None:
        return f"{side} {pick['line']}"
    if market == "batting_hits_yn":
        return f"{side} · 1+ hit"
    if market == "batting_homeRuns_yn":
        return f"{side} · 1+ home run"
    if market == "batting_stolenBases_yn":
        return f"{side} · 1+ stolen base"
    if market == "pitching_strikeouts_yn":
        return f"{side} · 1+ strikeout"
    if market == "pitching_earnedRuns_yn":
        return f"{side} · 1+ earned run"
    if market == "pitching_win_yn":
        return f"{side} · pitcher win"
    return side


def _freshness_label(timestamp: str | None, threshold_seconds: int = 900) -> str:
    """Human-readable age for a timestamp — same "Fresh (Xm ago)" /
    "Stale (Xh ago)" convention the admin dashboard uses for scan data,
    so Arbitrage/Middling cards read the numbers are just as live as the
    EV Picks are. 900s (15 min) default matches how often
    src/arb_middle_scan.py actually reconfirms these opportunities."""
    if not timestamp:
        return "Fresh"
    try:
        seen = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - seen).total_seconds()))
    except (TypeError, ValueError):
        return "Fresh"
    if age > threshold_seconds:
        return f"Stale ({age // 3600}h ago)" if age >= 3600 else f"Stale ({age // 60}m ago)"
    if age < 60:
        return "Fresh (just now)"
    return f"Fresh ({age // 60}m ago)"


def public_lock_view(row: dict) -> dict:
    """Project only non-sensitive pre-settlement fields for public display."""
    return {
        "matchup": row.get("matchup"),
        "event_start_time": row.get("event_start_time"),
        "event_status": row.get("event_status"),
        "scan_timestamp": row.get("scan_timestamp"),
        "official_rank": row.get("official_rank"),
    }


def _pick_bet_link_key(pick: dict) -> tuple | None:
    """(event_id, player_id, market_type, line, side, sportsbook) key for
    a single-leg pick -- None when event_id/player_id aren't available
    (never guessed)."""
    event_id, player_id = pick.get("event_id"), pick.get("player_id")
    if not event_id or not player_id:
        return None
    return (event_id, player_id, pick.get("market_type"), pick.get("line"), pick.get("side"), pick.get("sportsbook"))


def _attach_bet_links(
    conn, upcoming: list[dict], research: list[dict],
    active_arbitrage: list[dict], active_middles: list[dict],
) -> None:
    """Mutates each dict in place with its "Bet Now" deep link(s), via
    database.db_manager::get_bet_links -- see src/odds_api_props_parser.py
    and src/odds_api_game_parser.py for where the link is captured.
    Single-leg picks get `bet_link`; arbitrage (two sportsbooks) gets
    `bet_link_a`/`bet_link_b`; middles (Over/Under, two sportsbooks) get
    `bet_link_over`/`bet_link_under`. A leg with no matching link is left
    None -- the button for that leg simply doesn't render, never a
    guessed or stale one."""
    keys: list[tuple] = []
    for p in upcoming + research:
        k = _pick_bet_link_key(p)
        if k:
            keys.append(k)
    for opp in active_arbitrage:
        eid, pid = opp.get("event_id"), opp.get("player_id")
        if eid and pid:
            keys.append((eid, pid, opp.get("market_type"), opp.get("line"), opp.get("side_a"), opp.get("side_a_sportsbook")))
            keys.append((eid, pid, opp.get("market_type"), opp.get("line"), opp.get("side_b"), opp.get("side_b_sportsbook")))
    for opp in active_middles:
        eid, pid = opp.get("event_id"), opp.get("player_id")
        if eid and pid:
            keys.append((eid, pid, opp.get("market_type"), opp.get("over_line"), "OVER", opp.get("over_sportsbook")))
            keys.append((eid, pid, opp.get("market_type"), opp.get("under_line"), "UNDER", opp.get("under_sportsbook")))

    links = get_bet_links(conn, keys) if keys else {}

    for p in upcoming + research:
        k = _pick_bet_link_key(p)
        p["bet_link"] = links.get(k) if k else None
    for opp in active_arbitrage:
        eid, pid = opp.get("event_id"), opp.get("player_id")
        opp["bet_link_a"] = (
            links.get((eid, pid, opp.get("market_type"), opp.get("line"), opp.get("side_a"), opp.get("side_a_sportsbook")))
            if eid and pid else None
        )
        opp["bet_link_b"] = (
            links.get((eid, pid, opp.get("market_type"), opp.get("line"), opp.get("side_b"), opp.get("side_b_sportsbook")))
            if eid and pid else None
        )
    for opp in active_middles:
        eid, pid = opp.get("event_id"), opp.get("player_id")
        opp["bet_link_over"] = (
            links.get((eid, pid, opp.get("market_type"), opp.get("over_line"), "OVER", opp.get("over_sportsbook")))
            if eid and pid else None
        )
        opp["bet_link_under"] = (
            links.get((eid, pid, opp.get("market_type"), opp.get("under_line"), "UNDER", opp.get("under_sportsbook")))
            if eid and pid else None
        )


def _save_bet_now_settings_on_change() -> None:
    """on_change callback for the Bet Now settings widgets -- a
    module-level function (not a closure) reading the current account
    id out of session_state, matching Streamlit's own on_change
    calling convention. A no-op for a logged-out visitor (nothing to
    save against)."""
    account_id = st.session_state.get("_current_account_id")
    if not account_id:
        return
    conn = get_connection()
    try:
        state = st.session_state.get("bet_now_state")
        unit_usd = st.session_state.get("bet_now_unit_usd")
        save_account_settings(
            conn, account_id, (unit_usd or None), (None if state == "Not set" else state),
        )
    finally:
        conn.close()


def _render_auth_ui() -> None:
    """Sign-up / log-in forms, shown instead of the site whenever a
    real account is required (MLB_CUSTOMER_FREE_ACCESS=false) and the
    visitor isn't authorized yet."""
    st.subheader("Sign in to continue")
    tab_login, tab_signup = st.tabs(["Log In", "Sign Up"])

    with tab_login:
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Log In", type="primary", use_container_width=True)
        if submitted:
            conn = get_connection()
            try:
                account = log_in(conn, email, password)
                if account is None:
                    st.error("Invalid email or password.")
                else:
                    token = create_session(conn, account.account_id)
                    _set_session_cookie(token)
                    # Confirmed live 2026-09-15: extra_streamlit_components'
                    # cookie-set component call needs real wall-clock time
                    # to actually execute its JS in the browser before a
                    # rerun tears the component down -- an immediate
                    # st.rerun() here reliably wrote NOTHING to
                    # document.cookie in testing, a known, documented
                    # limitation of this library (not fixable by call
                    # order alone). This session doesn't depend on the
                    # cookie either way (see _current_account's
                    # st.session_state-first lookup) -- this brief pause
                    # is purely so the cookie is actually there for a
                    # FUTURE reload/visit.
                    time.sleep(1)
                    st.session_state["_session_token"] = token
                    st.session_state["_current_account_id"] = account.account_id
                    st.rerun()
            finally:
                conn.close()

    with tab_signup:
        with st.form("signup_form"):
            email = st.text_input("Email", key="signup_email")
            phone = st.text_input("Phone (optional)", key="signup_phone")
            password = st.text_input("Password", type="password", key="signup_password")
            password_confirm = st.text_input("Confirm password", type="password", key="signup_password_confirm")
            consent = st.checkbox(MARKETING_CONSENT_TEXT, value=False, key="signup_consent")
            st.caption("See our [Privacy Policy](?page=privacy) and [Terms of Service](?page=terms).")
            submitted = st.form_submit_button("Sign Up", type="primary", use_container_width=True)
        if submitted:
            if password != password_confirm:
                st.error("Passwords don't match.")
            else:
                conn = get_connection()
                try:
                    try:
                        account = sign_up(conn, email, phone, password, consent)
                    except SignUpError as exc:
                        st.error(str(exc))
                    else:
                        token = create_session(conn, account.account_id)
                        _set_session_cookie(token)
                        # See the matching comment in the log-in handler
                        # above -- this pause is purely so the cookie
                        # commits for a FUTURE reload; this session
                        # already doesn't depend on it via session_state.
                        time.sleep(1)
                        st.session_state["_session_token"] = token
                        st.session_state["_current_account_id"] = account.account_id
                        request_email_verification(conn, account, base_url=os.environ.get("SITE_BASE_URL"))
                        st.rerun()
                finally:
                    conn.close()


def _render_verify_email_banner(account: "Account | None") -> None:
    if account is not None and not account.email_verified:
        st.info("Verify your email to make sure you never miss an update — check your inbox for a link from us.")


def _handle_email_verification_query_param() -> None:
    """Handles a ?verify=<token> landing link from the verification
    email -- shown as a one-time banner, then the normal page continues
    underneath (never a dead end / separate page)."""
    token = st.query_params.get("verify")
    if not token:
        return
    conn = get_connection()
    try:
        if verify_email_token(conn, token):
            st.success("Email verified — thanks!")
        else:
            st.warning("That verification link is invalid or already used.")
    finally:
        conn.close()
    st.query_params.pop("verify", None)


_PRIVACY_POLICY_DRAFT = """
**DRAFT — not yet reviewed by a lawyer. Replace before relying on this for a real launch.**

We collect the email, phone number, and account settings you provide when you create an
account. Email and phone are used to operate your account (e.g. email verification) and,
only if you opted in at signup, for marketing messages from us. You can withdraw marketing
consent at any time (reply STOP to any text, or contact us to update your email preferences).
We do not sell your personal information to third parties.
"""

_TERMS_OF_SERVICE_DRAFT = """
**DRAFT — not yet reviewed by a lawyer. Replace before relying on this for a real launch.**

This site provides sports-betting research and analysis for informational purposes only.
It does not guarantee profit or place bets on your behalf. You are responsible for
complying with the laws and sportsbook terms that apply to you. By creating an account you
agree to these terms.
"""


def _render_policy_page(page: str) -> bool:
    """Returns True (and renders) if `page` is a policy-page query
    param -- caller should stop rendering the normal site in that case."""
    if page == "privacy":
        st.subheader("Privacy Policy")
        st.markdown(_PRIVACY_POLICY_DRAFT)
        return True
    if page == "terms":
        st.subheader("Terms of Service")
        st.markdown(_TERMS_OF_SERVICE_DRAFT)
        return True
    return False


@st.cache_data(ttl=30, show_spinner=False)
def load_customer_data(authorized: bool) -> dict:
    """Load only fields allowed for the request's entitlement level."""
    init_db()
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    horizon_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    baseline = get_performance_baseline(conn)
    try:
        settled = conn.execute("""
            SELECT hr.player_name, hr.matchup, hr.market_type, hr.side, hr.line,
                   hr.sportsbook, hr.offered_american_odds, hr.ev_pct,
                   hr.model_score, hr.scan_timestamp, hr.event_start_time,
                   hr.sport, hr.league, hr.fair_american_odds,
                   hr.confidence_score, hr.confidence_grade, hr.market_quality,
                   ms.settlement_status, ms.final_stat_value,
                   bu.profit_units, bu.risk_units,
                   cp.clv_probability, cp.closing_american, cp.closing_line,
                   cp.line_movement_direction
            FROM official_picks op
            JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
            JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
            LEFT JOIN bet_units bu ON bu.recommendation_id = hr.recommendation_id
            LEFT JOIN closing_prices cp ON cp.recommendation_id = hr.recommendation_id
            WHERE ms.settlement_status IN ('WIN','LOSS','PUSH','VOID','CANCELLED')
              AND op.pick_status = 'ACTIVE'
              AND hr.scan_timestamp >= ?
            ORDER BY hr.scan_timestamp DESC
        """, (baseline,)).fetchall()

        # This query intentionally contains no player, side, line, sportsbook,
        # odds, EV, or market fields. Public visitors only learn that a play
        # exists for a matchup and time.
        locked = conn.execute("""
            SELECT hr.matchup, hr.event_start_time, hr.event_status,
                   hr.scan_timestamp, op.official_rank
            FROM official_picks op
            JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
            LEFT JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
            WHERE hr.event_start_time IS NOT NULL
              AND hr.event_start_time >= ? AND hr.event_start_time <= ?
              AND op.pick_status = 'ACTIVE'
              AND (ms.recommendation_id IS NULL
                   OR ms.settlement_status IN ('UNRESOLVED','ungraded'))
            ORDER BY hr.event_start_time, op.official_rank
        """, (now_iso, horizon_iso)).fetchall()

        upcoming = []
        if authorized:
            upcoming = conn.execute("""
                SELECT hr.event_id, hr.player_id, hr.player_name, hr.matchup, hr.market_type, hr.side, hr.line,
                       hr.sportsbook, hr.offered_american_odds, hr.offered_decimal_odds, hr.ev_pct,
                       hr.model_score, hr.scan_timestamp, hr.event_start_time,
                       hr.sport, hr.league, hr.fair_american_odds,
                       hr.confidence_score, hr.confidence_grade, hr.market_quality,
                       op.outcome, op.official_rank
                FROM official_picks op
                JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
                LEFT JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
                WHERE hr.event_start_time IS NOT NULL
                  AND hr.event_start_time >= ? AND hr.event_start_time <= ?
                  AND op.pick_status = 'ACTIVE'
                  AND (ms.recommendation_id IS NULL
                       OR ms.settlement_status IN ('UNRESOLVED','ungraded'))
                ORDER BY hr.event_start_time, op.official_rank
            """, (now_iso, horizon_iso)).fetchall()
        research = []
        if authorized:
            research = conn.execute("""
                SELECT event_id, player_id, player_name, matchup, market_type, side, line, sportsbook,
                       offered_american_odds, offered_decimal_odds, ev_pct, yn_implied_prob_adv,
                       model_score, event_start_time, sport, league,
                       fair_american_odds, confidence_score, confidence_grade,
                       market_quality
                FROM historical_recommendations
                WHERE date(scan_timestamp) = ?
                  AND COALESCE(recommendation_tier, 'RESEARCH_ONLY') <> 'OFFICIAL_TRACKED'
                ORDER BY model_score DESC, ev_pct DESC
                LIMIT 25
            """, (get_today_in_configured_timezone(),)).fetchall()
        active_arbitrage = []
        graded_arbitrage = []
        active_middles = []
        graded_middles = []
        if authorized:
            from database.db_manager import (
                get_active_arbitrage_opportunities, get_graded_arbitrage_opportunities,
                get_active_middle_opportunities, get_graded_middle_opportunities,
            )
            active_arbitrage = get_active_arbitrage_opportunities(conn)
            graded_arbitrage = get_graded_arbitrage_opportunities(conn)
            # Only ever show a middle that's a CONFIRMED positive-EV bet
            # (verdict == WORTH_IT) -- 2026-09-15 (operator request): a
            # wide-but-unlikely window or one we genuinely can't estimate
            # (UNKNOWN) shouldn't be presented as something to bet on at
            # all, not even with a "not worth it"/"unknown" label.
            # graded_middles (settled history) is left unfiltered -- it's
            # a track record of what WAS shown, not a live pick list.
            active_middles = [
                m for m in get_active_middle_opportunities(conn) if m.get("verdict") == "WORTH_IT"
            ]
            graded_middles = get_graded_middle_opportunities(conn)

        upcoming_dicts = [dict(r) for r in upcoming]
        research_dicts = [dict(r) for r in research]
        if authorized:
            _attach_bet_links(conn, upcoming_dicts, research_dicts, active_arbitrage, active_middles)

        return {
            "settled": [dict(r) for r in settled],
            "locked": [dict(r) for r in locked],
            "upcoming": upcoming_dicts,
            "research": research_dicts,
            "active_arbitrage": active_arbitrage,
            "graded_arbitrage": graded_arbitrage,
            "active_middles": active_middles,
            "graded_middles": graded_middles,
        }
    finally:
        conn.close()


def _usable_with_books(
    opportunities: list[dict], available_books: set[str], book_fields: tuple[str, str],
) -> list[dict]:
    """Keep only opportunities where BOTH legs are at a book the viewer
    actually selected — an arbitrage or middle isn't usable unless you
    can place both bets, so an opportunity requiring even one book
    outside what you picked doesn't belong in the list."""
    field_a, field_b = book_fields
    return [
        o for o in opportunities
        if o.get(field_a) in available_books and o.get(field_b) in available_books
    ]


def _filter_by_game_status(opportunities: list[dict], status: str) -> list[dict]:
    """Split arbitrage/middle opportunities into pregame vs. live games
    (operator request 2026-09-10 — personal preference for pregame only,
    since live prices move fast and can be less reliable). "All" is a
    pass-through."""
    if status == "All":
        return opportunities
    want_live = status == "Live"
    return [o for o in opportunities if is_event_live(o.get("event_start_time")) == want_live]


def _apply_filters(rows: list[dict], filters: dict) -> list[dict]:
    """Filter a list of pick dicts by sport/sportsbook/market/EV/confidence/date."""
    out = rows
    if filters.get("sports"):
        wanted = set(filters["sports"])
        out = [r for r in out if (r.get("league") or "MLB").upper() in wanted]
    if filters.get("sportsbooks"):
        wanted = {b.lower() for b in filters["sportsbooks"]}
        out = [r for r in out if (r.get("sportsbook") or "").lower() in wanted]
    if filters.get("markets"):
        wanted = set(filters["markets"])
        out = [r for r in out if r.get("market_type") in wanted]
    if filters.get("min_ev") is not None:
        min_ev = filters["min_ev"]
        out = [r for r in out if (r.get("ev_pct") is None or r["ev_pct"] >= min_ev)]
    if filters.get("confidence_grades"):
        wanted = set(filters["confidence_grades"])
        out = [r for r in out if (r.get("confidence_grade") or "—") in wanted]
    if filters.get("date_from"):
        out = [r for r in out if (r.get("scan_timestamp") or "") >= filters["date_from"]]
    if filters.get("date_to"):
        out = [r for r in out if (r.get("scan_timestamp") or "") <= filters["date_to"]]
    return out


def render_pick_filters(rows: list[dict], key_prefix: str) -> dict:
    """Render a filter bar over the given pool of picks and return selections."""
    sports = sorted({(r.get("league") or "MLB").upper() for r in rows})
    books = sorted({r.get("sportsbook") for r in rows if r.get("sportsbook")})
    markets = sorted({r.get("market_type") for r in rows if r.get("market_type")})
    grades = sorted({r.get("confidence_grade") for r in rows if r.get("confidence_grade")})

    cols = st.columns(4)
    with cols[0]:
        f_sports = st.multiselect("Sport", sports, default=sports, key=f"{key_prefix}_sports")
    with cols[1]:
        f_books = st.multiselect("Sportsbook", books, key=f"{key_prefix}_books")
    with cols[2]:
        f_markets = st.multiselect("Market", markets, key=f"{key_prefix}_markets",
                                    format_func=_market_label)
    with cols[3]:
        f_grades = st.multiselect("Confidence", grades, key=f"{key_prefix}_grades")
    f_min_ev = st.slider("Minimum EV%", -5.0, 20.0, -5.0, 0.5, key=f"{key_prefix}_ev")

    return {
        "sports": f_sports, "sportsbooks": f_books, "markets": f_markets,
        "confidence_grades": f_grades,
        "min_ev": f_min_ev if f_min_ev > -5.0 else None,
    }


def performance_series(rows: list[dict], period: str = "ALL") -> pd.DataFrame:
    """Calculate cumulative expected units versus actual units."""
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["posted"] = pd.to_datetime(frame["scan_timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["posted"]).sort_values("posted")
    if period in ("7D", "30D"):
        days = 7 if period == "7D" else 30
        frame = frame[frame["posted"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)]
    frame["risk_units"] = pd.to_numeric(frame["risk_units"], errors="coerce").fillna(0.0)
    frame["profit_units"] = pd.to_numeric(frame["profit_units"], errors="coerce").fillna(0.0)
    frame["ev_pct"] = pd.to_numeric(frame["ev_pct"], errors="coerce").fillna(0.0)
    frame["expected_units"] = frame["risk_units"] * frame["ev_pct"] / 100.0
    frame["expected_cumulative"] = frame["expected_units"].cumsum()
    frame["actual_cumulative"] = frame["profit_units"].cumsum()
    return frame.set_index("posted")[["expected_cumulative", "actual_cumulative"]]


def _render_full_pick(pick: dict, settled: bool = False, state: str | None = None, unit_usd: float | None = None) -> None:
    side_line = _side_line_label(pick)
    market = pick.get("market_type") or ""
    if market.endswith("_yn"):
        advantage = pick.get("yn_implied_prob_adv")
        edge_text = f"{advantage:+.2f} pp price advantage" if advantage is not None else "Price advantage tracked"
    else:
        edge = pick.get("ev_pct")
        edge_text = f"{edge:+.2f}% EV" if edge is not None else "Edge tracked"
    status = _settled_status(pick) or "OPEN"
    result_class = status.lower() if status.lower() in {"win", "loss", "push", "void"} else ""
    final = f" · Final: {pick['final_stat_value']}" if pick.get("final_stat_value") is not None else ""
    units = f" · {pick['profit_units']:+.2f}u" if pick.get("profit_units") is not None else ""
    stake = f"Stake: {pick['risk_units']:.2f}u" if pick.get("risk_units") is not None else "Stake: —"
    result_label = status if status in {"WIN", "LOSS", "PUSH", "VOID", "CANCELLED"} else "OPEN"
    result_style = "result-win" if status == "WIN" else "result-loss" if status == "LOSS" else ""

    detail_bits = [
        f"Fair odds: {_fair_odds_label(pick)}",
        f"Confidence: {_confidence_label(pick)}",
    ]
    if pick.get("market_quality"):
        detail_bits.append(f"Market: {_market_label(pick['market_quality'])}")
    detail_line = " · ".join(detail_bits)

    closing_bits = []
    if settled:
        clv = pick.get("clv_probability")
        if clv is not None:
            closing_bits.append(f"CLV: {clv:+.2%}")
        elif pick.get("line_movement_direction"):
            closing_bits.append(f"Line moved: {pick['line_movement_direction']}")
        if pick.get("closing_american") is not None:
            closing_line = pick.get("closing_line")
            close_txt = f"Closed {closing_line} {pick['closing_american']:+d}" if closing_line is not None \
                else f"Closed {pick['closing_american']:+d}"
            closing_bits.append(close_txt)
    closing_line_html = f'<div class="pick-meta">{" · ".join(closing_bits)}</div>' if closing_bits else ""
    bet_now_html = (
        "" if settled else
        _bet_now_html(pick.get("sportsbook", "").title(), pick.get("bet_link"), state, _suggested_stake_units(pick), unit_usd)
    )

    st.markdown(f"""
    <div class="pick {'settled' if settled else ''} {result_class}">
      <div class="pick-title">{pick.get('player_name') or 'Top Play'}</div>
      <div class="pick-meta">{_league_badge(pick)} · {pick.get('matchup','')} · {_market_label(pick.get('market_type',''))} · {side_line}</div>
      <div class="pick-meta">{pick.get('sportsbook','')} {pick.get('offered_american_odds','')} · <span class="edge">{edge_text}</span></div>
      <div class="pick-meta">{detail_line}</div>
      {closing_line_html}
      <div class="unit-line">{stake} · Result: <span class="{result_style}">{result_label}{final}{units}</span></div>
      {bet_now_html}
    </div>
    """, unsafe_allow_html=True)


def _render_locked_pick(lock: dict) -> None:
    st.markdown(f"""
    <div class="pick locked">
      <div class="pick-title">{lock.get('matchup') or 'Game'}</div>
      <div class="pick-meta">{lock.get('event_start_time','')[:16]} · Top Model Play</div>
      <div class="lock-copy">VIP PICK AVAILABLE 🔒</div>
      <div class="pick-meta">Unlock the exact player and wager before first pitch.</div>
    </div>
    """, unsafe_allow_html=True)


def _cumulative_chart(rows: list[dict], label: str) -> None:
    """Small cumulative-units chart shared by the Arbitrage and Middling
    pages -- each track record is kept separate from the main EV Picks
    performance chart and from each other, on purpose."""
    if not rows:
        st.caption(f"No settled {label.lower()} yet.")
        return
    df = pd.DataFrame({
        "Date": [r["graded_at"] for r in rows],
        "Profit": [r["profit_units"] or 0 for r in rows],
    })
    df["Cumulative"] = df["Profit"].cumsum()
    df["Date"] = pd.to_datetime(df["Date"])
    total = df["Cumulative"].iloc[-1]
    color = "#16a34a" if total >= 0 else "#dc2626"
    st.markdown(f"""
    <div class="results-panel">
      <div class="results-eyebrow">{label} Track Record</div>
      <div class="results-number" style="color:{color};">{total:+.2f}u</div>
      <div class="results-caption">Cumulative result of every settled {label.lower()} opportunity, in order.</div>
    </div>
    """, unsafe_allow_html=True)
    chart = alt.Chart(df).mark_area(
        line={"color": color, "strokeWidth": 2.5}, color=color, opacity=0.16, interpolate="step-after",
    ).encode(
        x=alt.X("Date:T", title=None,
                axis=alt.Axis(grid=False, labelColor="#6b7280", tickColor="#e5e7eb", domainColor="#e5e7eb")),
        y=alt.Y("Cumulative:Q", title="Cumulative units", stack=None,
                axis=alt.Axis(grid=True, gridColor="#f0f1f3", labelColor="#6b7280", titleColor="#6b7280")),
    ).configure_view(strokeWidth=0).configure(background="transparent")
    st.altair_chart(chart, use_container_width=True)


def _render_arbitrage_card(opp: dict, state: str | None = None) -> None:
    pick_label = f"{_market_label(opp['market_type'])}" + (
        f" {opp['line']}" if opp.get("line") is not None else ""
    )
    fresh = _freshness_label(opp.get("last_seen_at"))
    game_time = format_event_start_local(opp.get("event_start_time"))
    status_label = "🔴 LIVE" if is_event_live(opp.get("event_start_time")) else "PREGAME"
    matchup_text = opp.get("matchup") or "Matchup unavailable"
    # No dollar-stake suggestion here -- arbitrage's two legs are sized
    # to a balanced *pct* of a notional total (side_a_stake_pct/
    # side_b_stake_pct), not this platform's unit system, so there's no
    # unit_usd-based amount to compute; the button still gets the
    # customer straight to the right bet.
    bet_now_a = _bet_now_html(f"{opp['side_a_sportsbook'].title()} ({opp['side_a']})", opp.get("bet_link_a"), state, None, None)
    bet_now_b = _bet_now_html(f"{opp['side_b_sportsbook'].title()} ({opp['side_b']})", opp.get("bet_link_b"), state, None, None)
    st.markdown(f"""
    <div class="pick">
      <div class="pick-title">{opp.get('player_name') or opp.get('matchup') or pick_label}</div>
      <div class="pick-meta">{_league_badge(opp)} · {matchup_text} · {pick_label} · <span class="edge">{fresh}</span></div>
      <div class="pick-meta">{status_label} · Game starts: {game_time}</div>
      <div class="pick-meta">{opp['side_a']} · {opp['side_a_sportsbook']} {opp['side_a_price']:+d}
        ({opp['side_a_stake_pct']:.0%} stake)</div>
      {bet_now_a}
      <div class="pick-meta">{opp['side_b']} · {opp['side_b_sportsbook']} {opp['side_b_price']:+d}
        ({opp['side_b_stake_pct']:.0%} stake)</div>
      {bet_now_b}
      <div class="unit-line">Guaranteed: <span class="result-win">+{opp['guaranteed_roi_pct']:.2f}%</span></div>
    </div>
    """, unsafe_allow_html=True)


_MIDDLE_VERDICT_BADGE = {
    "WORTH_IT": '<span class="pill result-win" style="border-color:var(--win)">✅ WORTH IT</span>',
    "NOT_WORTH_IT": '<span class="pill result-loss" style="border-color:var(--loss)">❌ NOT WORTH IT</span>',
    "UNKNOWN": '<span class="pill">❓ UNKNOWN</span>',
}


def _render_middle_card(opp: dict, state: str | None = None, unit_usd: float | None = None) -> None:
    fresh = _freshness_label(opp.get("last_seen_at"))
    game_time = format_event_start_local(opp.get("event_start_time"))
    status_label = "🔴 LIVE" if is_event_live(opp.get("event_start_time")) else "PREGAME"
    hit_prob = opp.get("hit_probability")
    true_ev = opp.get("true_ev_pct")
    stake = opp.get("recommended_stake_units")
    verdict = opp.get("verdict")
    verdict_badge = _MIDDLE_VERDICT_BADGE.get(verdict, _MIDDLE_VERDICT_BADGE["UNKNOWN"])
    if hit_prob is not None and true_ev is not None:
        ev_class = "result-win" if true_ev > 0 else "result-loss"
        ev_line = (
            f'<div class="pick-meta">Est. hit chance: {hit_prob * 100:.1f}% '
            f'· True EV: <span class="{ev_class}">{true_ev:+.2f}%</span></div>'
        )
    else:
        ev_line = '<div class="pick-meta">Hit chance not estimable for this window (thin alt-line data)</div>'
    # Same "Stake: X.XXu" phrasing/placement as the main EV-pick cards
    # (see _render_pick_card) -- one consistent, unmissable place to
    # read the bet size before it settles, site-wide. A stake number is
    # only ever shown for a WORTH_IT verdict -- recommended_stake_units
    # is None (not 0) for anything else, so this never mistakes "no
    # recommendation" for "bet 0 units."
    if verdict == "WORTH_IT" and stake:
        stake_line = f"Stake: {stake:.2f}u"
    else:
        stake_line = "Stake: — (not worth betting)"
    matchup_text = opp.get("matchup") or "Matchup unavailable"
    # Split the recommended total stake across the two legs by their own
    # sizing (over_stake_pct/under_stake_pct sum to 1, by construction --
    # see src/middling.py -- the same split that makes a single-leg win
    # pay the same amount regardless of which leg hits).
    over_units = (stake * opp.get("over_stake_pct", 0.5)) if (verdict == "WORTH_IT" and stake) else None
    under_units = (stake * opp.get("under_stake_pct", 0.5)) if (verdict == "WORTH_IT" and stake) else None
    bet_now_over = _bet_now_html(f"{opp['over_sportsbook'].title()} (Over)", opp.get("bet_link_over"), state, over_units, unit_usd)
    bet_now_under = _bet_now_html(f"{opp['under_sportsbook'].title()} (Under)", opp.get("bet_link_under"), state, under_units, unit_usd)
    st.markdown(f"""
    <div class="pick">
      <div class="pick-title">{opp.get('player_name') or opp.get('matchup') or _market_label(opp['market_type'])} {verdict_badge}</div>
      <div class="pick-meta">{_league_badge(opp)} · {matchup_text} · {_market_label(opp['market_type'])} · <span class="edge">{fresh}</span></div>
      <div class="pick-meta">{status_label} · Game starts: {game_time}</div>
      <div class="pick-meta">Over {opp['over_line']} · {opp['over_sportsbook']} {opp['over_price']:+d}</div>
      {bet_now_over}
      <div class="pick-meta">Under {opp['under_line']} · {opp['under_sportsbook']} {opp['under_price']:+d}</div>
      {bet_now_under}
      <div class="unit-line">{stake_line}</div>
      <div class="unit-line">Worst case: <span class="result-loss">{opp['worst_case_roi_pct']:+.2f}%</span>
        · Best case: <span class="result-win">+{opp['best_case_roi_pct']:.2f}%</span></div>
      {ev_line}
    </div>
    """, unsafe_allow_html=True)


if _render_policy_page(st.query_params.get("page", "")):
    st.stop()

_handle_email_verification_query_param()

current_account = _current_account()
if current_account is not None:
    st.session_state["_current_account_id"] = current_account.account_id

authorized = _authorized_request(current_account)
require_account = os.getenv("MLB_CUSTOMER_FREE_ACCESS", "true").strip().lower() == "false"

if require_account and not authorized:
    st.markdown("""
    <div class="topnav">
      <div class="topnav-brand">
        <span class="topnav-mark">VIP</span>
        <span class="topnav-word">Sharp Market Intelligence</span>
      </div>
    </div>
    """, unsafe_allow_html=True)
    _render_auth_ui()
    st.stop()

try:
    data = load_customer_data(authorized)
except Exception:
    logger.exception("Customer data load failed")
    st.error("The model data is temporarily unavailable. Please check back shortly.")
    st.stop()

if "view_mode" not in st.session_state:
    st.session_state.view_mode = None

today = datetime.now(timezone.utc).strftime("%B %d, %Y")
st.markdown(f"""
<div class="topnav">
  <div class="topnav-brand">
    <span class="topnav-mark">VIP</span>
    <span class="topnav-word">Sharp Market Intelligence</span>
  </div>
</div>
<div class="hero">
  <div class="eyebrow">MLB · NFL · WNBA</div>
  <h1>Multi-book odds analysis</h1>
  <p>Every price is screened against fair value and market quality before it's shown. Every result — win or loss — is tracked and published in full.</p>
  <span class="pill">{today} · {'FULL ACCESS' if authorized else 'PUBLIC VIEW'}</span>
</div>
""", unsafe_allow_html=True)

if current_account is not None:
    _render_verify_email_banner(current_account)
    if st.button("Log out", key="log_out_btn"):
        _log_out()
        st.rerun()

if st.session_state.view_mode is not None:
    if st.button("← All Options", key="back_to_menu"):
        st.session_state.view_mode = None
        st.rerun()
    st.divider()

# ==================================================================
# Menu — three boxes, pick a mode
# ==================================================================
if st.session_state.view_mode is None:
    st.subheader("What do you want to see?")
    st.caption("Three independent ways to use this model. Pick one.")
    menu_cols = st.columns(3)

    with menu_cols[0]:
        with st.container(border=True):
            st.markdown("### 📈 EV Picks")
            st.markdown(
                "The model's own top-scored plays, checked against Pinnacle and the "
                "wider market. A verified, nothing-hidden track record."
            )
            st.metric("Live today", len(data["upcoming"]) if authorized else len(data["locked"]))
            if st.button("View EV Picks →", key="choose_ev", use_container_width=True, type="primary"):
                st.session_state.view_mode = "ev"
                st.rerun()

    with menu_cols[1]:
        with st.container(border=True):
            st.markdown("### ⚖️ Arbitrage")
            st.markdown(
                "Cross-book price mismatches. Bet both sides yourself — a guaranteed "
                "profit no matter which side wins."
            )
            st.metric("Live now", len(data["active_arbitrage"]))
            if st.button("View Arbitrage →", key="choose_arb", use_container_width=True, type="primary"):
                st.session_state.view_mode = "arbitrage"
                st.rerun()

    with menu_cols[2]:
        with st.container(border=True):
            st.markdown("### ⚡ Middling")
            st.markdown(
                "Over at one line, Under at a higher line, two different books. Small "
                "capped risk, big upside if the number lands in between."
            )
            st.metric("Live now", len(data["active_middles"]))
            if st.button("View Middling →", key="choose_mid", use_container_width=True, type="primary"):
                st.session_state.view_mode = "middling"
                st.rerun()

# ==================================================================
# EV Picks
# ==================================================================
elif st.session_state.view_mode == "ev":
    if not authorized:
        st.info("Top plays are posted when the slate qualifies. Subscriber access unlocks the exact wager before the game; settled picks become public automatically for full accountability.")
        st.subheader("Today's Top Picks")
        if data["locked"]:
            for lock in data["locked"]:
                _render_locked_pick(lock)
            st.button("Unlock Today's Picks", type="primary", use_container_width=True, disabled=True)
        else:
            st.success("No Top Picks Yet")
            st.caption("The model has not identified an opportunity meeting today's qualification standards.")
    else:
        bet_now_state, bet_now_unit_usd = _render_bet_now_settings(current_account)
        st.subheader("Today's Top Picks — Upcoming")
        if data["upcoming"]:
            with st.expander("Filter upcoming picks", expanded=False):
                up_filters = render_pick_filters(data["upcoming"], "upcoming")
            filtered_upcoming = _apply_filters(data["upcoming"], up_filters)
            if filtered_upcoming:
                for pick in filtered_upcoming:
                    _render_full_pick(pick, state=bet_now_state, unit_usd=bet_now_unit_usd)
            else:
                st.caption("No upcoming picks match the current filters.")
        else:
            st.success("No Top Picks Yet")
            st.caption("The model has not identified an opportunity meeting today's qualification standards.")
        if data["research"]:
            with st.expander("Full Board"):
                for pick in data["research"]:
                    _render_full_pick(pick, state=bet_now_state, unit_usd=bet_now_unit_usd)

    st.divider()
    st.subheader("Verified Track Record — Past Picks")
    st.caption("Settled Top Picks only. Winners and losses are included equally; no results are manually selected or hidden.")
    if data["settled"]:
        with st.expander("Filter settled picks", expanded=False):
            settled_filters = render_pick_filters(data["settled"], "settled")
        filtered_settled = _apply_filters(data["settled"], settled_filters)

        summary = performance_summary(filtered_settled)
        period = st.radio("Performance period", ["7D", "30D", "ALL"], horizontal=True, index=2)
        series = performance_series(filtered_settled, period)
        if not series.empty:
            chart_df = series.rename(columns={
                "expected_cumulative": "Expected Units",
                "actual_cumulative": "Actual Units",
            }).reset_index().rename(columns={"posted": "Date"})

            period_units = chart_df["Actual Units"].iloc[-1]
            positive = period_units >= 0
            line_color = "#16a34a" if positive else "#dc2626"

            st.markdown(f"""
            <div class="results-panel">
              <div class="results-eyebrow">Real Results — {period}</div>
              <div class="results-number" style="color:{line_color};">{period_units:+.2f}u</div>
              <div class="results-caption">Cumulative result if every Top Pick were followed at its recorded stake.
              Every settled pick counts — wins and losses included equally, nothing hidden or cherry-picked.</div>
            </div>
            """, unsafe_allow_html=True)

            area = alt.Chart(chart_df).mark_area(
                line={"color": line_color, "strokeWidth": 2.5},
                color=line_color, opacity=0.16, interpolate="monotone",
            ).encode(
                x=alt.X("Date:T", title=None,
                        axis=alt.Axis(grid=False, labelColor="#6b7280", tickColor="#e5e7eb", domainColor="#e5e7eb")),
                y=alt.Y("Actual Units:Q", title="Cumulative units", stack=None,
                        axis=alt.Axis(grid=True, gridColor="#f0f1f3", labelColor="#6b7280", titleColor="#6b7280")),
                tooltip=[alt.Tooltip("Date:T", title="Date"), alt.Tooltip("Actual Units:Q", format="+.2f")],
            )
            expected_line = alt.Chart(chart_df).mark_line(
                color="#9ca3af", strokeDash=[4, 3], strokeWidth=1.6, interpolate="monotone", opacity=0.85,
            ).encode(
                x="Date:T",
                y="Expected Units:Q",
                tooltip=[alt.Tooltip("Date:T", title="Date"),
                         alt.Tooltip("Expected Units:Q", format="+.2f", title="Expected Units")],
            )
            st.caption("Solid area: actual settled profit. Dashed line: expected units from each pick's recorded EV and stake.")
            st.altair_chart(
                (area + expected_line).properties(height=300)
                .configure_view(strokeWidth=0)
                .configure(background="transparent"),
                width="stretch",
            )

        if filtered_settled:
            with st.expander(f"View all {len(filtered_settled)} settled picks", expanded=False):
                for pick in filtered_settled[:10]:
                    _render_full_pick(pick, settled=True)
        else:
            st.caption("No settled picks match the current filters.")

        st.markdown("#### Performance Dashboard")
        cols = st.columns(6)
        cols[0].metric("Record", f"{summary['wins']}-{summary['losses']}-{summary['pushes']}")
        cols[1].metric("Settled Picks", summary["settled"])
        cols[2].metric("Units", f"{summary['units_won']:+.2f}")
        cols[3].metric("ROI", f"{summary['roi']:.1%}" if summary["units_risked"] else "—")
        cols[4].metric("Avg CLV", f"{summary['avg_clv_probability']:+.2%}" if summary["avg_clv_probability"] is not None else "—")
        cols[5].metric("Beat Close %", f"{summary['pct_beating_close']:.1%}" if summary["pct_beating_close"] is not None else "—")
        st.caption(f"Average EV at recommendation: {summary['avg_ev_pct']:+.2f}%")

        with st.expander("Performance breakdown by sport / market / sportsbook / confidence / EV"):
            for field, label in [("sport", "Sport"), ("market_type", "Market"),
                                  ("sportsbook", "Sportsbook"), ("confidence_grade", "Confidence Grade")]:
                groups = breakdown_by_field(filtered_settled, field)
                if not groups:
                    continue
                st.markdown(f"**By {label}**")
                table = pd.DataFrame([
                    {label: key, "Record": f"{g['wins']}-{g['losses']}-{g['pushes']}",
                     "Units": round(g["units_won"], 2), "ROI": f"{g['roi']:.1%}" if g["units_risked"] else "—",
                     "Avg EV%": g["avg_ev_pct"], "Beat Close %":
                         f"{g['pct_beating_close']:.1%}" if g["pct_beating_close"] is not None else "—"}
                    for key, g in sorted(groups.items())
                ])
                st.dataframe(table, hide_index=True, use_container_width=True)

            ev_bucketed = [{**r, "_ev_bucket": assign_bucket(r.get("ev_pct") or 0.0, EV_BUCKETS)}
                           for r in filtered_settled]
            ev_groups = breakdown_by_field(ev_bucketed, "_ev_bucket")
            if ev_groups:
                st.markdown("**By EV Bucket**")
                table = pd.DataFrame([
                    {"EV Bucket": key, "Record": f"{g['wins']}-{g['losses']}-{g['pushes']}",
                     "Units": round(g["units_won"], 2), "ROI": f"{g['roi']:.1%}" if g["units_risked"] else "—"}
                    for key, g in sorted(ev_groups.items())
                ])
                st.dataframe(table, hide_index=True, use_container_width=True)
    else:
        st.info("BUILDING VERIFIED TRACK RECORD · Performance appears after Top Picks settle.")

# ==================================================================
# Arbitrage
# ==================================================================
elif st.session_state.view_mode == "arbitrage":
    st.subheader("⚖️ Arbitrage — guaranteed profit")
    st.caption(
        "Cross-book price mismatches — bet both sides yourself, at the two books shown. "
        "The combined stake guarantees a profit no matter which side wins. Not a \"pick\" "
        "to follow; you place both bets."
    )
    if not authorized:
        st.info("Subscriber access unlocks live arbitrage opportunities.")
    else:
        arb_bet_now_state, _ = _render_bet_now_settings(current_account)
        if not data["active_arbitrage"]:
            st.success("No arbitrage opportunities right now.")
            st.caption("Rechecked every ~15 minutes as odds move.")
        else:
            arb_status = st.selectbox(
                "Game status", ["Pregame", "Live", "All"], index=0, key="cust_arb_status",
                help="Pregame: before first pitch/kickoff. Live: game already underway — "
                     "prices move faster and can be less reliable.",
            )
            arb_status_filtered = _filter_by_game_status(data["active_arbitrage"], arb_status)
            if not arb_status_filtered:
                st.info(f"No {arb_status.lower()} arbitrage opportunities right now.")
            else:
                arb_book_fields = ("side_a_sportsbook", "side_b_sportsbook")
                arb_selected_books = render_sportsbook_picker(sorted(TRACKED_BOOKMAKERS.split(",")), key_prefix="cust_arb")
                arb_usable = _usable_with_books(arb_status_filtered, arb_selected_books, arb_book_fields)
                if not arb_usable:
                    st.warning("No arbitrage opportunities usable with the sportsbooks selected above.")
                else:
                    if len(arb_usable) < len(arb_status_filtered):
                        st.caption(f"{len(arb_usable)} of {len(arb_status_filtered)} opportunities usable with your selected books.")
                    arb_cols = st.columns(2)
                    for i, opp in enumerate(arb_usable):
                        with arb_cols[i % 2]:
                            _render_arbitrage_card(opp, state=arb_bet_now_state)
        st.divider()
        _cumulative_chart(data["graded_arbitrage"], "Arbitrage")

# ==================================================================
# Middling
# ==================================================================
elif st.session_state.view_mode == "middling":
    st.subheader("⚡ Middling — small guaranteed risk, big upside")
    st.caption(
        "Over at a lower line, Under at a higher line, two different books. If the final "
        "number lands in the window, both bets win. Outside it, the guaranteed worst case "
        "is capped small — never a full loss on both legs."
    )
    if not authorized:
        st.info("Subscriber access unlocks live middling opportunities.")
    else:
        mid_bet_now_state, mid_bet_now_unit_usd = _render_bet_now_settings(current_account)
        if not data["active_middles"]:
            st.success("No middle opportunities right now.")
            st.caption("Rechecked every ~15 minutes as odds move.")
        else:
            mid_status = st.selectbox(
                "Game status", ["Pregame", "Live", "All"], index=0, key="cust_mid_status",
                help="Pregame: before first pitch/kickoff. Live: game already underway — "
                     "prices move faster and can be less reliable.",
            )
            mid_status_filtered = _filter_by_game_status(data["active_middles"], mid_status)
            if not mid_status_filtered:
                st.info(f"No {mid_status.lower()} middle opportunities right now.")
            else:
                mid_book_fields = ("over_sportsbook", "under_sportsbook")
                mid_selected_books = render_sportsbook_picker(sorted(TRACKED_BOOKMAKERS.split(",")), key_prefix="cust_mid")
                mid_usable = _usable_with_books(mid_status_filtered, mid_selected_books, mid_book_fields)
                if not mid_usable:
                    st.warning("No middle opportunities usable with the sportsbooks selected above.")
                else:
                    if len(mid_usable) < len(mid_status_filtered):
                        st.caption(f"{len(mid_usable)} of {len(mid_status_filtered)} opportunities usable with your selected books.")
                    mid_cols = st.columns(2)
                    for i, opp in enumerate(mid_usable):
                        with mid_cols[i % 2]:
                            _render_middle_card(opp, state=mid_bet_now_state, unit_usd=mid_bet_now_unit_usd)
        st.divider()
        _cumulative_chart(data["graded_middles"], "Middling")

st.divider()
features = st.columns(4)
for col, title, body in zip(features, ["Multi-book scan", "Fair value", "Sharp reference", "Accountability"], [
    "Prices compared across sportsbooks.", "Conservative probability and EV checks.", "Pinnacle used only on exact valid matches.", "Pregame prices, CLV, and results are preserved.",
]):
    with col:
        st.markdown(f'<div class="feature"><div class="feature-title">{title}</div><div class="section-note">{body}</div></div>', unsafe_allow_html=True)

_books_seen = sorted({
    (r.get("sportsbook") or "").strip()
    for pool in (data["settled"], data["upcoming"], data["research"])
    for r in pool
    if r.get("sportsbook")
})
_books_line = " &nbsp;·&nbsp; ".join(_books_seen) if _books_seen else "Books populate once the model has scanned live odds."

st.markdown(f"""
<div class="footer-band">
  <div class="footer-label">Leagues Covered &middot; Books Scanned &middot; Updated Automatically</div>
  <div class="footer-books">MLB &nbsp;·&nbsp; NFL &nbsp;·&nbsp; WNBA &nbsp;&mdash;&nbsp; {_books_line}</div>
</div>
""", unsafe_allow_html=True)

st.caption("This platform does not guarantee profit, place bets, or present Full Board signals as Top Picks.")
