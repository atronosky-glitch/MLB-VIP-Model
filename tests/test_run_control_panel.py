"""Tests for run_control_panel.py, the safe local-only Streamlit
launcher (section 12). Never actually spawns Streamlit -- subprocess.call
is mocked throughout; these tests confirm the command/env this launcher
builds, not real server behavior."""

import importlib.util
import sys
from pathlib import Path
from unittest import mock

_MODULE_PATH = Path(__file__).resolve().parents[1] / "run_control_panel.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_control_panel", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(argv, env_overrides=None, monkeypatch_env=None):
    module = _load_module()
    with mock.patch("subprocess.call", return_value=0) as mocked_call:
        old_argv = sys.argv
        sys.argv = ["run_control_panel.py"] + argv
        try:
            module.main()
        finally:
            sys.argv = old_argv
    return mocked_call


class TestSafeLocalBinding:
    def test_always_binds_to_127_0_0_1(self):
        mocked_call = _run([])
        cmd = mocked_call.call_args.args[0]
        assert "--server.address" in cmd
        assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"

    def test_default_port_is_8501(self):
        mocked_call = _run([])
        cmd = mocked_call.call_args.args[0]
        assert cmd[cmd.index("--server.port") + 1] == "8501"

    def test_port_flag_is_respected(self):
        mocked_call = _run(["--port", "9999"])
        cmd = mocked_call.call_args.args[0]
        assert cmd[cmd.index("--server.port") + 1] == "9999"

    def test_never_references_the_cloud_deployment_config(self):
        mocked_call = _run([])
        cmd = mocked_call.call_args.args[0]
        joined = " ".join(cmd)
        assert "streamlit_config" not in joined
        assert "0.0.0.0" not in joined

    def test_launches_control_panel_module(self):
        mocked_call = _run([])
        cmd = mocked_call.call_args.args[0]
        assert "src/control_panel.py" in cmd

    def test_strips_unsafe_env_vars_before_launch(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_SERVER_ADDRESS", "0.0.0.0")
        mocked_call = _run([])
        passed_env = mocked_call.call_args.kwargs["env"]
        assert "STREAMLIT_SERVER_ADDRESS" not in passed_env

    def test_browser_server_address_is_also_localhost(self):
        mocked_call = _run([])
        cmd = mocked_call.call_args.args[0]
        assert cmd[cmd.index("--browser.serverAddress") + 1] == "127.0.0.1"
