"""Creator / referral codes: attribution only, never blocks signup, never trusts raw input."""

import pytest

import database.db_manager as dbm
from database.db_manager import get_connection, init_db
from src import referrals
from src.customer_accounts import sign_up


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "ref.db"
    monkeypatch.setattr(dbm, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    init_db(str(path))
    c = get_connection()
    yield c
    c.close()


def _signup(conn, email, code=None, consent=False):
    return sign_up(conn, email, None, "Str0ng-passw0rd!", consent, referral_code=code)


def _stored(conn, email):
    return conn.execute("SELECT referral_code FROM customer_accounts WHERE email = ?", (email,)).fetchone()["referral_code"]


class TestCodes:
    def test_normalize(self):
        assert referrals.normalize_code(" alex10 ") == "ALEX10"
        for bad in (None, 5, "", "a", "bad code", "x" * 40, "<script>", "a;drop"):
            assert referrals.normalize_code(bad) is None

    def test_create_validates_and_rejects_duplicates(self, conn):
        assert referrals.create_code(conn, "alex10", " Alex ", "20% rev share") == "ALEX10"
        with pytest.raises(ValueError):
            referrals.create_code(conn, "ALEX10", "Someone")
        with pytest.raises(ValueError):
            referrals.create_code(conn, "bad code", "X")
        with pytest.raises(ValueError):
            referrals.create_code(conn, "OKAY", "  ")

    def test_resolve_only_active_existing_codes(self, conn):
        referrals.create_code(conn, "ALEX10", "Alex")
        assert referrals.resolve_code(conn, "alex10") == "ALEX10"
        assert referrals.resolve_code(conn, "NOPE") is None
        referrals.set_active(conn, "ALEX10", False)
        assert referrals.resolve_code(conn, "ALEX10") is None


class TestSignupAttribution:
    def test_valid_code_is_recorded_and_counted(self, conn):
        referrals.create_code(conn, "ALEX10", "Alex")
        _signup(conn, "a@example.com", "alex10", consent=True)
        _signup(conn, "b@example.com", "ALEX10")
        _signup(conn, "c@example.com", None)
        assert _stored(conn, "a@example.com") == "ALEX10" and _stored(conn, "c@example.com") is None
        s = referrals.creator_stats(conn)[0]
        assert s["signups"] == 2 and s["signups_7d"] == 2 and s["marketing_opt_in"] == 1 and s["verified"] == 0

    @pytest.mark.parametrize("code", ["UNKNOWN", "bad code!", "x" * 80, "'; DROP TABLE customer_accounts;--", ""])
    def test_bad_or_unknown_codes_never_block_signup(self, conn, code):
        acct = _signup(conn, "z@example.com", code)
        assert acct.email == "z@example.com" and _stored(conn, "z@example.com") is None

    def test_inactive_code_is_not_attributed(self, conn):
        referrals.create_code(conn, "OLD", "Old")
        referrals.set_active(conn, "OLD", False)
        _signup(conn, "q@example.com", "OLD")
        assert _stored(conn, "q@example.com") is None

    def test_existing_signup_path_without_a_code_still_works(self, conn):
        assert _signup(conn, "plain@example.com").account_id


def test_customer_site_captures_ref_param_and_prefills_the_signup_box(tmp_path, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    path = tmp_path / "site.db"
    monkeypatch.setattr(dbm, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MLB_CUSTOMER_FREE_ACCESS", "true")
    init_db(str(path))
    c = get_connection()
    referrals.create_code(c, "ALEX10", "Alex")
    c.close()
    import extra_streamlit_components as stx

    class _Cookies:
        def __init__(self, *a, **k):
            pass

        def set(self, *a, **k):
            pass

        def delete(self, *a, **k):
            pass

        def get(self, *a, **k):
            return None

    monkeypatch.setattr(stx, "CookieManager", _Cookies)
    import streamlit as st
    st.cache_data.clear()
    at = AppTest.from_file(str(Path("src/customer_view.py")), default_timeout=90)
    at.query_params["ref"] = "alex10"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.text_input(key="signup_ref_code").value == "ALEX10"
    at.text_input(key="signup_email").set_value("fan@example.com")
    at.text_input(key="signup_password").set_value("Str0ng-passw0rd!")
    at.text_input(key="signup_password_confirm").set_value("Str0ng-passw0rd!")
    next(b for b in at.button if b.label == "Sign Up").click()
    at.run()
    assert not at.exception
    c = get_connection()
    assert _stored(c, "fan@example.com") == "ALEX10"
    c.close()
    st.cache_data.clear()


def test_creators_admin_page_creates_a_code_and_lists_stats(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    path = tmp_path / "admin.db"
    monkeypatch.setattr(dbm, "DB_PATH", path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    init_db(str(path))
    monkeypatch.setenv("REF_TEST_DB", str(path))
    src = ("import os, sqlite3\nimport streamlit as st\nfrom src.referrals import render_creators_admin\n"
           "c = sqlite3.connect(os.environ['REF_TEST_DB'])\nc.row_factory = sqlite3.Row\n"
           "render_creators_admin(c, st)\n")
    at = AppTest.from_string(src, default_timeout=30).run()
    assert not at.exception and any("No creator codes yet" in i.value for i in at.info)
    at.text_input(key="creator_new_code").set_value("alex10")
    at.text_input(key="creator_new_name").set_value("Alex")
    next(b for b in at.button if b.label == "Create code").click()
    at.run()
    assert not at.exception and any("Created ALEX10" in s.value for s in at.success)
    at.text_input(key="creator_new_code").set_value("bad code")
    next(b for b in at.button if b.label == "Create code").click()
    at.run()
    assert any("Codes are 2-32" in e.value for e in at.error)
