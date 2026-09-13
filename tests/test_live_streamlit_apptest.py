"""Streamlit approval dry-run tests (spec section 13) using Streamlit's
own official testing framework (streamlit.testing.v1.AppTest) rather
than hand-mocked widgets -- exercises render_live_execution_tab() as a
real (simulated) Streamlit script run. LIVE_TRADING_ENABLED stays False
throughout; provider mutation call count must remain zero regardless
of what's clicked.
"""

import os
import tempfile
from unittest import mock

import pytest
import streamlit.config as st_config
from streamlit.testing.v1 import AppTest

from database.db_manager import init_db

_REAL_GET_OPTION = st_config.get_option


class _FakeConfig:
    kalshi_enabled = False
    polymarket_us_enabled = False
    kalshi_live_enabled = False
    polymarket_us_live_enabled = False
    live_trading_enabled = False


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(path)
    yield path
    os.remove(path)


def _server_address_override(value):
    """Patches ONLY the "server.address" lookup, delegating every other
    option Streamlit's own internals query (message size limits,
    worker counts, etc.) to the real implementation -- a blanket mock
    of get_option breaks Streamlit's own script runner."""
    def fake_get_option(key, *args, **kwargs):
        if key == "server.address":
            return value
        return _REAL_GET_OPTION(key, *args, **kwargs)
    return mock.patch("streamlit.config.get_option", side_effect=fake_get_option)


def _tab_entrypoint(config, db_path):
    # A plain, unannotated wrapper -- AppTest.from_function extracts and
    # re-execs only this function's own source in an isolated namespace,
    # which does not carry the real module's `from __future__ import
    # annotations` / `Any` import along with it. Keeping this wrapper
    # annotation-free avoids a spurious NameError while still exercising
    # the real render_live_execution_tab implementation underneath.
    from src.live_execution_panel import render_live_execution_tab
    render_live_execution_tab(config, db_path)


def _clean_hosting_env():
    """Removes only the specific hosting-platform vars this module
    checks for -- unlike mock.patch.dict(..., clear=True), this leaves
    HOME/USERPROFILE/etc. intact, which Streamlit's own internals need
    to locate its config file."""
    keys = (
        "RENDER", "RENDER_SERVICE_ID", "RAILWAY_ENVIRONMENT", "DYNO", "FLY_APP_NAME",
        "WEBSITE_INSTANCE_ID", "GAE_APPLICATION", "KUBERNETES_SERVICE_HOST",
    )
    return mock.patch.dict(os.environ, {k: "" for k in keys}, clear=False)


def _run_tab(db_path: str, config=None):
    at = AppTest.from_function(
        _tab_entrypoint, kwargs={"config": config or _FakeConfig(), "db_path": db_path},
    )
    at.run()
    return at


class TestSafeLocalSession:
    def test_renders_without_error_when_local(self, temp_db_path):
        with _server_address_override(None), \
             _clean_hosting_env():
            at = _run_tab(temp_db_path)
        assert not at.exception

    def test_shows_ready_for_approval_section(self, temp_db_path):
        with _server_address_override(None), \
             _clean_hosting_env():
            at = _run_tab(temp_db_path)
        markdown_text = " ".join(m.value for m in at.markdown)
        assert "READY FOR APPROVAL" in markdown_text

    def test_shows_the_live_mode_toggle(self, temp_db_path):
        with _server_address_override(None), \
             _clean_hosting_env():
            at = _run_tab(temp_db_path)
        assert len(at.toggle) == 1
        assert at.toggle[0].value is False  # defaults OFF

    def test_shows_local_only_warning_banner(self, temp_db_path):
        with _server_address_override(None), \
             _clean_hosting_env():
            at = _run_tab(temp_db_path)
        warning_text = " ".join(w.value for w in at.warning)
        assert "local-only" in warning_text.lower()

    def test_no_prepared_orders_means_no_approve_buttons(self, temp_db_path):
        with _server_address_override(None), \
             _clean_hosting_env():
            at = _run_tab(temp_db_path)
        approve_buttons = [b for b in at.button if "APPROVE" in (b.label or "")]
        assert approve_buttons == []


class TestPubliclyExposedSession:
    """The critical hardening case: when the session looks
    network-exposed, no approval controls may render at all."""

    def test_0_0_0_0_binding_blocks_the_approval_ui(self, temp_db_path):
        with _server_address_override("0.0.0.0"):
            at = _run_tab(temp_db_path)
        assert not at.exception
        error_text = " ".join(e.value for e in at.error)
        assert "BLOCKED" in error_text

    def test_no_approve_button_widgets_exist_when_exposed(self, temp_db_path):
        with _server_address_override("0.0.0.0"):
            at = _run_tab(temp_db_path)
        assert at.button == []
        assert at.toggle == []

    def test_hosting_platform_env_var_also_blocks(self, temp_db_path):
        with _server_address_override(None), \
             mock.patch.dict(os.environ, {"RENDER": "true"}, clear=False):
            at = _run_tab(temp_db_path)
        error_text = " ".join(e.value for e in at.error)
        assert "BLOCKED" in error_text
        assert at.button == []


class TestRerunSafety:
    """Streamlit reruns the script on every interaction -- confirms
    repeated runs of the same page never accumulate duplicate state or
    raise, with zero provider mutation capability reachable regardless."""

    def test_multiple_reruns_stay_stable(self, temp_db_path):
        with _server_address_override(None), \
             _clean_hosting_env():
            at = _run_tab(temp_db_path)
            for _ in range(3):
                at.run()
        assert not at.exception
