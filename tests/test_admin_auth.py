"""Admin gate for the operator dashboard (src/admin_auth.py)."""

import pytest

from src import admin_auth
from src.admin_auth import AdminGate, access_mode


class TestAdminGate:
    def test_correct_password_passes_wrong_fails(self):
        g = AdminGate()
        assert g.attempt("s3cret", "s3cret", now=0) is True
        assert g.attempt("wrong", "s3cret", now=0) is False

    def test_empty_expected_password_never_authenticates(self):
        g = AdminGate()
        assert g.attempt("", "", now=0) is False
        assert g.attempt("anything", "", now=0) is False

    def test_lockout_after_max_failures_applies_to_everyone_even_the_right_password(self):
        g = AdminGate(max_failures=3, lockout_seconds=60)
        for _ in range(3):
            assert g.attempt("bad", "s3cret", now=10) is False
        assert g.is_locked(now=11) > 0
        assert g.attempt("s3cret", "s3cret", now=20) is False       # locked: even correct is refused
        assert g.attempt("s3cret", "s3cret", now=10 + 61) is True   # lock expired

    def test_success_resets_the_failure_counter(self):
        g = AdminGate(max_failures=3, lockout_seconds=60)
        g.attempt("bad", "p", now=0)
        g.attempt("bad", "p", now=0)
        assert g.attempt("p", "p", now=0) is True
        g.attempt("bad", "p", now=0)
        g.attempt("bad", "p", now=0)
        assert g.is_locked(now=0) == 0                              # counter restarted, not carried over

    def test_non_ascii_password_does_not_raise(self):
        assert AdminGate().attempt("pässwörd", "pässwörd", now=0) is True


class TestAccessMode:
    def test_production_without_a_password_fails_closed(self, monkeypatch):
        monkeypatch.delenv(admin_auth.ADMIN_PASSWORD_ENV, raising=False)
        monkeypatch.setenv("MLB_ENVIRONMENT", "production")
        assert access_mode() == "locked_no_password"

    def test_development_without_a_password_is_open_with_a_warning(self, monkeypatch):
        monkeypatch.delenv(admin_auth.ADMIN_PASSWORD_ENV, raising=False)
        monkeypatch.setenv("MLB_ENVIRONMENT", "development")
        assert access_mode() == "open_dev"

    def test_a_configured_password_always_requires_login(self, monkeypatch):
        monkeypatch.setenv(admin_auth.ADMIN_PASSWORD_ENV, "x")
        for env in ("production", "development", ""):
            monkeypatch.setenv("MLB_ENVIRONMENT", env)
            assert access_mode() == "password"


class TestControlPanelIsGated:
    def test_control_panel_calls_require_admin_before_any_other_streamlit_content(self):
        from pathlib import Path
        src = Path("src/control_panel.py").read_text(encoding="utf-8")
        gate = src.index("require_admin()")
        assert gate < src.index("st.tabs([")
        assert gate < src.index("st.markdown(")
        assert gate > src.index("st.set_page_config(")           # set_page_config must stay first

    def test_dashboard_service_declares_the_admin_password_as_a_secret(self):
        from pathlib import Path
        yaml = Path("render.yaml").read_text(encoding="utf-8")
        i = yaml.index("MLB_ADMIN_PASSWORD")
        assert "sync: false" in yaml[i:i + 60]

    def test_locked_dashboard_renders_nothing_else(self, monkeypatch):
        from streamlit.testing.v1 import AppTest
        monkeypatch.delenv(admin_auth.ADMIN_PASSWORD_ENV, raising=False)
        monkeypatch.setenv("MLB_ENVIRONMENT", "production")

        def script():
            from src.admin_auth import require_admin
            import streamlit as st
            require_admin()
            st.write("SECRET-ADMIN-CONTENT")

        at = AppTest.from_function(script, default_timeout=30)
        at.run()
        assert not at.exception
        assert any("locked" in e.value.lower() for e in at.error)
        assert all("SECRET-ADMIN-CONTENT" not in str(m.value) for m in at.markdown)

    def test_login_flow_unlocks_only_with_the_right_password(self, monkeypatch):
        from streamlit.testing.v1 import AppTest
        monkeypatch.setenv(admin_auth.ADMIN_PASSWORD_ENV, "correct-horse")
        monkeypatch.setenv("MLB_ENVIRONMENT", "production")
        admin_auth._GATE.__init__()

        def script():
            from src.admin_auth import require_admin
            import streamlit as st
            require_admin()
            st.write("ADMIN-CONTENT")

        at = AppTest.from_function(script, default_timeout=30)
        at.run()
        assert not any("ADMIN-CONTENT" in str(m.value) for m in at.markdown)
        at.text_input[0].set_value("nope"); at.button[0].click(); at.run()
        assert any("Incorrect" in e.value for e in at.error)
        assert not any("ADMIN-CONTENT" in str(m.value) for m in at.markdown)
        at.text_input[0].set_value("correct-horse"); at.button[0].click(); at.run()
        assert any("ADMIN-CONTENT" in str(m.value) for m in at.markdown)
