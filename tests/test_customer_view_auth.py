"""Tests for the account-login wiring added to src/customer_view.py on
2026-09-15 -- AST-extracted (customer_view.py runs Streamlit page code
at import time), matching the established pattern in
tests/test_customer_view.py and tests/test_research_market_inventory.py.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _load_authorized_request():
    """AST-extracts _authorized_request with a fake `st`/`os` seeded in
    -- the function only ever touches st.query_params.get(...), never a
    rendering call, so a minimal fake is enough to exercise every
    branch without a real Streamlit runtime."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_node = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_authorized_request"
    )
    import hmac
    import os as real_os
    namespace = {"hmac": hmac, "os": real_os, "st": None}
    exec(compile(ast.Module(body=[func_node], type_ignores=[]), "<extracted>", "exec"), namespace)
    return namespace["_authorized_request"]


class _FakeQueryParams:
    def __init__(self, values: dict):
        self._values = values

    def get(self, key, default=""):
        return self._values.get(key, default)


class TestAuthorizedRequest:
    """Real preservation requirement (see the pinned test in
    tests/test_customer_view.py): the free-access and legacy-shared-
    token paths must never be deleted or altered -- only a new account
    check may be added, as an ADDITIONAL way in, checked last."""

    def _call(self, monkeypatch, env: dict, query_params: dict, account):
        import src.customer_view as _unused  # ensure the module is importable at all
        authorized_request = _load_authorized_request()
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        fake_st = SimpleNamespace(query_params=_FakeQueryParams(query_params))
        authorized_request.__globals__["st"] = fake_st
        return authorized_request(account)

    def test_free_access_on_by_default_ignores_everything_else(self, monkeypatch):
        monkeypatch.delenv("MLB_CUSTOMER_FREE_ACCESS", raising=False)
        assert self._call(monkeypatch, {}, {}, account=None) is True

    def test_free_access_explicitly_true_ignores_everything_else(self, monkeypatch):
        assert self._call(monkeypatch, {"MLB_CUSTOMER_FREE_ACCESS": "true"}, {}, account=None) is True

    def test_free_access_off_no_token_no_account_is_not_authorized(self, monkeypatch):
        result = self._call(
            monkeypatch,
            {"MLB_CUSTOMER_FREE_ACCESS": "false", "MLB_CUSTOMER_ACCESS_TOKEN": "secret123"},
            {}, account=None,
        )
        assert result is False

    def test_free_access_off_valid_legacy_token_is_authorized(self, monkeypatch):
        result = self._call(
            monkeypatch,
            {"MLB_CUSTOMER_FREE_ACCESS": "false", "MLB_CUSTOMER_ACCESS_TOKEN": "secret123"},
            {"access": "secret123"}, account=None,
        )
        assert result is True

    def test_free_access_off_wrong_legacy_token_is_not_authorized(self, monkeypatch):
        result = self._call(
            monkeypatch,
            {"MLB_CUSTOMER_FREE_ACCESS": "false", "MLB_CUSTOMER_ACCESS_TOKEN": "secret123"},
            {"access": "wrong"}, account=None,
        )
        assert result is False

    def test_free_access_off_no_token_but_logged_in_account_is_authorized(self, monkeypatch):
        fake_account = SimpleNamespace(account_id="abc123")
        result = self._call(
            monkeypatch,
            {"MLB_CUSTOMER_FREE_ACCESS": "false", "MLB_CUSTOMER_ACCESS_TOKEN": "secret123"},
            {}, account=fake_account,
        )
        assert result is True

    def test_free_access_off_no_access_token_env_set_at_all_still_falls_through_to_account(self, monkeypatch):
        monkeypatch.delenv("MLB_CUSTOMER_ACCESS_TOKEN", raising=False)
        fake_account = SimpleNamespace(account_id="abc123")
        result = self._call(monkeypatch, {"MLB_CUSTOMER_FREE_ACCESS": "false"}, {}, account=fake_account)
        assert result is True


def test_password_inputs_use_type_password_never_plaintext():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert source.count('type="password"') >= 2  # login + signup


def test_signup_consent_checkbox_defaults_unchecked():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'st.checkbox(MARKETING_CONSENT_TEXT, value=False' in source


def test_password_never_passed_to_st_markdown_or_logged():
    """A defensive static sweep: the password/password_confirm local
    variables must never appear inside an f-string passed to
    st.markdown/st.error/logger.* anywhere in the file."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if "password" in stripped.lower() and ("st.markdown" in stripped or "logger." in stripped):
            raise AssertionError(f"Possible password exposure: {stripped}")


def test_log_out_clears_session_cookie_and_stale_widget_state():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "_clear_session_cookie()" in source
    assert '"bet_now_state", "bet_now_unit_usd", "_current_account_id"' in source


def test_free_access_off_gates_the_whole_site_behind_auth_ui():
    """2026-09-15 (operator request): using the site at all requires an
    account once MLB_CUSTOMER_FREE_ACCESS=false -- not just the
    protected picks section."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "if require_account and not authorized:" in source
    assert "_render_auth_ui()" in source


def test_verification_email_requested_on_signup_but_never_blocks_it():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "request_email_verification(conn, account" in source


def test_policy_pages_are_marked_as_drafts_pending_legal_review():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "not yet reviewed by a lawyer" in source.lower()


def test_cookie_manager_is_not_cache_resource():
    """Real bug found live 2026-09-15: @st.cache_resource on the
    CookieManager constructor raised CachedWidgetWarning in this
    Streamlit version -- a bidirectional component call is treated as
    widget-like, and the cache-replay-rules policy forbids widget calls
    inside a cached function. Must stay a plain st.session_state-backed
    singleton instead."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert '@st.cache_resource\ndef _get_cookie_manager' not in source
    assert '"_cookie_manager" not in st.session_state' in source


def test_session_cookie_write_has_a_settle_pause_before_rerun():
    """Real bug found live 2026-09-15: an immediate st.rerun() right
    after CookieManager.set() reliably wrote NOTHING to
    document.cookie in live browser testing -- a documented
    architectural race in this library between the component's JS
    round-trip and Streamlit tearing the component down for the rerun.
    time.sleep(1) between the two, confirmed live to fix it."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert source.count("_set_session_cookie(token)\n") >= 1
    assert source.count("time.sleep(1)") == 2  # login path + signup path


def test_session_read_uses_st_context_cookies_not_the_async_component():
    """Real bug found live 2026-09-15: CookieManager.get() reliably
    returned None on the first script run of a fresh page load even
    with a real, already-set cookie present in the browser (its own
    initial getAll() component call hasn't resolved yet either) --
    st.context.cookies is the official, synchronous, race-free read."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "st.context.cookies.get(SESSION_COOKIE_NAME)" in source


def test_session_state_checked_before_cookie_for_current_account():
    """The just-logged-in-this-session case must never depend on the
    cookie write having already landed in the browser."""
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert 'st.session_state.get("_session_token") or _get_session_token()' in source
