"""END-TO-END customer acceptance test, driving the REAL customer site
(src/customer_view.py) with Streamlit's AppTest against a temporary database:

  new visitor -> sign up -> (logged in) -> current official picks -> history
  -> model performance -> Auto-Bet page with nothing connected -> empty
  My Performance -> log out -> log back in -> another user is isolated.

The only stand-in is the browser cookie component (it needs a real browser);
authentication itself, the database, the queries and every page render are
the production code paths.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import database.db_manager as dbm
from database.db_manager import init_db

PASSWORD = "correct-horse-battery-1"


@pytest.fixture()
def site(tmp_path, monkeypatch):
    path = tmp_path / "e2e.db"
    monkeypatch.setattr(dbm, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MLB_CUSTOMER_FREE_ACCESS", "true")
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    init_db(str(path))

    import extra_streamlit_components as stx

    class _NoBrowserCookies:
        def __init__(self, *a, **k):
            pass

        def set(self, *a, **k):
            pass

        def delete(self, *a, **k):
            pass

        def get(self, *a, **k):
            return None

    monkeypatch.setattr(stx, "CookieManager", _NoBrowserCookies)
    import streamlit as st
    st.cache_data.clear()          # load_customer_data is cached for 30s process-wide
    yield path
    st.cache_data.clear()


def _db(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _seed_pick(path, rec_id, *, player, settled=None, start_hours=3):
    """An OFFICIAL pick exactly as the pipeline freezes it (recommendation row +
    ACTIVE official_picks row), optionally graded."""
    now = datetime.now(timezone.utc)
    conn = _db(path)
    conn.execute(
        """INSERT INTO historical_recommendations (
               recommendation_id, fingerprint, event_id, player_id, player_name, matchup, market_type,
               market_form, period, line, side, sportsbook, offered_american_odds, offered_decimal_odds,
               offered_implied_prob, fair_prob, ev_pct, model_score, rec_status, rec_eligible,
               recommendation_tier, scan_timestamp, event_start_time, league, sport)
           VALUES (?, ?, ?, ?, ?, 'Away Team @ Home Team', 'strikeouts', 'ou', 'full_game', 6.5, 'OVER',
                   'DraftKings', 110, 2.10, 0.476, 0.53, 11.3, 8.4, 'STRONG_EDGE', 1, 'OFFICIAL_TRACKED',
                   ?, ?, 'MLB', 'baseball')""",
        (rec_id, f"fp-{rec_id}", f"E-{rec_id}", f"P-{rec_id}", player, now.isoformat(),
         (now + timedelta(hours=start_hours)).isoformat()),
    )
    conn.execute("INSERT INTO official_picks (recommendation_id, tier, official_rank, pick_status) "
                 "VALUES (?, 'OFFICIAL_TRACKED', 1, 'ACTIVE')", (rec_id,))
    if settled:
        conn.execute(
            "INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, final_stat_value, settled_at) "
            "VALUES (?, ?, ?, 7, ?)", (str(uuid.uuid4()), rec_id, settled, now.isoformat()))
        conn.execute("UPDATE historical_recommendations SET event_start_time = ? WHERE recommendation_id = ?",
                     ((now - timedelta(hours=5)).isoformat(), rec_id))
    conn.commit()
    conn.close()


def _open():
    at = AppTest.from_file(str(Path("src/customer_view.py")), default_timeout=90)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def _text(at):
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption] + [s.value for s in at.subheader]
    parts += [i.value for i in at.info] + [w.value for w in at.warning] + [s.value for s in at.success]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    return "\n".join(str(p) for p in parts)


def _sign_up(at, email="new.user@example.com"):
    at.text_input(key="signup_email").set_value(email)
    at.text_input(key="signup_password").set_value(PASSWORD)
    at.text_input(key="signup_password_confirm").set_value(PASSWORD)
    _button(at, "Sign Up").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]


class TestNewVisitorToLoggedInCustomer:
    def test_landing_page_is_free_and_offers_account_creation(self, site):
        at = _open()
        assert "What do you want to see?" in _text(at)
        assert any("create a free account" in e.label for e in at.expander)     # sign-up is reachable
        assert not any(b.label == "Log out" for b in at.button)                 # anonymous
        text = _text(at).lower()
        for gate in ("subscribe", "payment required", "upgrade to", "premium", "pay now", "checkout"):
            assert gate not in text, gate                                         # no paywall language

    def test_signup_creates_a_real_account_and_logs_the_user_in(self, site):
        at = _open()
        _sign_up(at)
        conn = _db(site)
        row = dict(conn.execute("SELECT * FROM customer_accounts WHERE email = 'new.user@example.com'").fetchone())
        assert row["password_hash"].startswith("$2") and PASSWORD not in str(row)      # bcrypt, never plaintext
        assert conn.execute("SELECT COUNT(*) AS n FROM customer_sessions").fetchone()["n"] == 1
        conn.close()
        assert any(b.label == "Log out" for b in at.button)
        assert any(m.label == "Kalshi" for m in at.metric) and any(m.label == "Polymarket" for m in at.metric)

    def test_duplicate_signup_is_refused_with_a_clear_message(self, site):
        at = _open()
        _sign_up(at)
        at2 = _open()
        at2.text_input(key="signup_email").set_value("NEW.user@example.com")   # case variant
        at2.text_input(key="signup_password").set_value(PASSWORD)
        at2.text_input(key="signup_password_confirm").set_value(PASSWORD)
        _button(at2, "Sign Up").click()
        at2.run()
        assert any("already exists" in e.value for e in at2.error)
        assert _db(site).execute("SELECT COUNT(*) AS n FROM customer_accounts").fetchone()["n"] == 1

    def test_invalid_login_is_generic_and_valid_login_works_after_logout(self, site):
        at = _open()
        _sign_up(at)
        _button(at, "Log out").click()
        at.run()
        assert not any(b.label == "Log out" for b in at.button)                # logged out
        assert _db(site).execute("SELECT COUNT(*) AS n FROM customer_sessions").fetchone()["n"] == 0

        at.text_input(key="login_email").set_value("new.user@example.com")
        at.text_input(key="login_password").set_value("wrong-password-1")
        _button(at, "Log In").click()
        at.run()
        assert any(e.value == "Invalid email or password." for e in at.error)

        at.text_input(key="login_email").set_value("NEW.User@Example.com")     # case-insensitive email
        at.text_input(key="login_password").set_value(PASSWORD)
        _button(at, "Log In").click()
        at.run()
        assert not at.exception
        assert any(b.label == "Log out" for b in at.button)                    # back in


class TestCustomerCanUseTheModel:
    def test_current_official_picks_history_and_performance(self, site):
        _seed_pick(site, "rec-open", player="Open Pick Guy")
        _seed_pick(site, "rec-won", player="Settled Winner", settled="WIN")
        _seed_pick(site, "rec-lost", player="Settled Loser", settled="LOSS")
        at = _open()
        _sign_up(at)
        _button(at, "View EV Picks →").click()
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        text = _text(at)
        assert "Open Pick Guy" in text                                          # current official pick
        assert "Verified Track Record" in text                                  # history/performance section
        assert "Settled Winner" in text and "Settled Loser" in text            # settled results shown

    def test_unofficial_research_rows_are_not_presented_as_official_picks(self, site):
        _seed_pick(site, "rec-official", player="Official Guy")
        conn = _db(site)
        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO historical_recommendations (recommendation_id, fingerprint, event_id, player_id,
                   player_name, matchup, market_type, market_form, period, line, side, sportsbook,
                   offered_american_odds, offered_decimal_odds, offered_implied_prob, ev_pct, model_score,
                   rec_status, rec_eligible, recommendation_tier, scan_timestamp, event_start_time, league, sport)
               VALUES ('rec-research', 'fp-r', 'E-r', 'P-r', 'Research Only Guy', 'A @ B', 'strikeouts', 'ou',
                       'full_game', 5.5, 'OVER', 'FanDuel', -105, 1.95, 0.51, 3.0, 5.0, 'POSITIVE_EDGE', 1,
                       'RESEARCH_ONLY', ?, ?, 'MLB', 'baseball')""",
            (now.isoformat(), (now + timedelta(hours=2)).isoformat()))
        conn.commit()
        conn.close()
        at = _open()
        _button(at, "View EV Picks →").click()
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        inside_full_board = [m.value for e in at.expander if e.label == "Full Board" for m in e.markdown]
        outside = " ".join(m.value for m in at.markdown if m.value not in set(inside_full_board))
        assert "Official Guy" in outside
        assert "Research Only Guy" not in outside                               # never presented as an official pick
        assert any("Research Only Guy" in v for v in inside_full_board)         # only inside the labeled Full Board

    def test_no_picks_gives_a_useful_empty_state_not_a_crash(self, site):
        at = _open()
        _button(at, "View EV Picks →").click()
        at.run()
        assert not at.exception
        assert "No Top Picks Yet" in _text(at)

    def test_empty_states_for_a_brand_new_customer(self, site):
        at = _open()
        _sign_up(at)
        for label, expect in (("View My Performance →", "My Performance"), ("Manage Auto-Bet →", "Auto-Bet")):
            at.session_state["view_mode"] = None
            at.run()
            _button(at, label).click()
            at.run()
            assert not at.exception, (label, [e.value for e in at.exception])
            assert expect in _text(at)
        # My Performance for a customer with no executions renders zeros, never a crash
        at.session_state["view_mode"] = "performance"
        at.run()
        assert not at.exception
        metrics = {m.label: m.value for m in at.metric}
        assert metrics.get("Filled Bets") == "0"
        assert "No Auto-Bet activity" in _text(at)


class TestIsolationAndAdmin:
    def test_a_second_customer_never_sees_the_first_customers_performance(self, site):
        at = _open()
        _sign_up(at, "a@example.com")
        conn = _db(site)
        account_a = conn.execute("SELECT account_id FROM customer_accounts WHERE email = 'a@example.com'").fetchone()[0]
        conn.execute(
            """INSERT INTO customer_autobet_executions (execution_id, account_id, recommendation_id, matchup, side,
                   stake_usd, requested_quantity, filled_quantity, avg_fill_price, status, mode, platform, fill_source)
               VALUES ('x1', ?, 'rec-a', 'SECRET-A-MATCHUP', 'YES', 10, 20, 20, 0.5, 'EXECUTED', 'LIVE',
                       'kalshi', 'PLATFORM_FILLS')""", (account_a,))
        conn.commit()
        conn.close()
        b = _open()
        _sign_up(b, "b@example.com")
        b.session_state["view_mode"] = "performance"
        b.run()
        assert not b.exception
        assert "SECRET-A-MATCHUP" not in _text(b) and "SECRET-A-MATCHUP" not in str(b.dataframe)
        assert {m.label: m.value for m in b.metric}.get("Filled Bets") == "0"

    def test_customer_site_has_no_admin_surface(self):
        src = Path("src/customer_view.py").read_text(encoding="utf-8")
        for admin_only in ("control_panel", "live_execution_panel", "render_live_execution_tab",
                           "run_pipeline", "subprocess", "MLB_ADMIN_PASSWORD"):
            assert admin_only not in src, admin_only

    def test_dashboard_refuses_to_render_without_an_admin_password_in_production(self, monkeypatch):
        monkeypatch.setenv("MLB_ENVIRONMENT", "production")
        monkeypatch.delenv("MLB_ADMIN_PASSWORD", raising=False)
        at = AppTest.from_file(str(Path("src/control_panel.py")), default_timeout=60)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        assert any("locked" in e.value.lower() for e in at.error)
        assert len(at.tabs) == 0                                               # no admin tabs rendered
