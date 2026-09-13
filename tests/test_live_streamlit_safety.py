"""Tests for src/live_execution_panel.py's public-exposure detection --
Stage 4.1 hardening triggered by discovering this repo's own
docs/CLOUD_DEPLOYMENT.md and docs/DEPLOYMENT.md document running
control_panel.py with `--server.address 0.0.0.0` for phone/cloud
access. The Live Execution tab must refuse to show approval controls
when it looks like it isn't running as a private local session.
"""

import os
from unittest import mock

from src.live_execution_panel import _detect_public_exposure_risk


class TestLocalAddressDetection:
    def test_default_none_address_is_treated_as_local(self):
        with mock.patch("streamlit.config.get_option", return_value=None):
            assert _detect_public_exposure_risk() is None

    def test_localhost_is_treated_as_local(self):
        with mock.patch("streamlit.config.get_option", return_value="localhost"):
            assert _detect_public_exposure_risk() is None

    def test_127_0_0_1_is_treated_as_local(self):
        with mock.patch("streamlit.config.get_option", return_value="127.0.0.1"):
            assert _detect_public_exposure_risk() is None

    def test_0_0_0_0_is_flagged_as_risky(self):
        with mock.patch("streamlit.config.get_option", return_value="0.0.0.0"):
            result = _detect_public_exposure_risk()
            assert result is not None
            assert "0.0.0.0" in result

    def test_arbitrary_bound_address_is_flagged(self):
        with mock.patch("streamlit.config.get_option", return_value="192.168.1.50"):
            assert _detect_public_exposure_risk() is not None

    def test_get_option_raising_does_not_crash_and_falls_through(self):
        with mock.patch("streamlit.config.get_option", side_effect=RuntimeError("no config yet")):
            # Should not raise -- falls through to env-var checks, which
            # (with a clean environment) report no risk.
            with mock.patch.dict(os.environ, {}, clear=True):
                assert _detect_public_exposure_risk() is None


class TestHostingPlatformEnvDetection:
    def test_render_env_var_is_flagged(self):
        with mock.patch("streamlit.config.get_option", return_value=None), \
             mock.patch.dict(os.environ, {"RENDER": "true"}, clear=False):
            result = _detect_public_exposure_risk()
            assert result is not None
            assert "RENDER" in result

    def test_railway_env_var_is_flagged(self):
        with mock.patch("streamlit.config.get_option", return_value=None), \
             mock.patch.dict(os.environ, {"RAILWAY_ENVIRONMENT": "production"}, clear=False):
            assert _detect_public_exposure_risk() is not None

    def test_no_hosting_env_vars_is_not_flagged(self):
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in (
                "RENDER", "RENDER_SERVICE_ID", "RAILWAY_ENVIRONMENT", "DYNO", "FLY_APP_NAME",
                "WEBSITE_INSTANCE_ID", "GAE_APPLICATION", "KUBERNETES_SERVICE_HOST",
            )
        }
        with mock.patch("streamlit.config.get_option", return_value=None), \
             mock.patch.dict(os.environ, clean_env, clear=True):
            assert _detect_public_exposure_risk() is None
