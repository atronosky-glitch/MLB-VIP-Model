"""Tests for src/discord_delivery.py's run_webhook_test()/`test-webhooks`
CLI command (2026-09-15) -- verifies each configured Discord webhook is
reachable without touching dedup state or sending real alert content."""

import urllib.error
from unittest import mock

from src.discord_delivery import main
from src.discord_delivery import test_mlb_discord_connection as run_mlb_discord_test
from src.discord_delivery import test_webhooks as run_webhook_test


class _FakeConfig:
    discord_webhook_urls = ""
    discord_webhook_urls_arb_middle = ""
    discord_webhook_urls_middle = ""


class TestTestWebhooks:
    def test_none_configured_reports_nothing_configured(self):
        result = run_webhook_test(config=_FakeConfig())
        assert result["any_configured"] is False
        for info in result["channels"].values():
            assert info["configured"] is False

    def test_sends_one_message_per_configured_url(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1,https://discord.com/api/webhooks/ev2"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True) as mocked:
            result = run_webhook_test(config=config)
        assert mocked.call_count == 2
        assert result["channels"]["ev_picks"] == {
            "configured": True, "urls_tested": 2, "passed": 2, "failed": 0,
        }

    def test_failed_send_is_reported_per_url(self):
        config = _FakeConfig()
        config.discord_webhook_urls_middle = "https://discord.com/api/webhooks/mid"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=False):
            result = run_webhook_test(config=config)
        assert result["channels"]["middles"] == {
            "configured": True, "urls_tested": 1, "passed": 0, "failed": 1,
        }

    def test_never_touches_dedup_or_real_content(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True) as mocked, \
             mock.patch("database.db_manager.mark_recommendations_alerted") as mocked_mark:
            run_webhook_test(config=config)
        mocked_mark.assert_not_called()
        sent_text = mocked.call_args[0][1]
        assert "test" in sent_text.lower()
        assert "STRONG_EDGE" not in sent_text  # never real recommendation content

    def test_independent_channels_report_independently(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        config.discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb1"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True):
            result = run_webhook_test(config=config)
        assert result["channels"]["ev_picks"]["configured"] is True
        assert result["channels"]["arbitrage"]["configured"] is True
        assert result["channels"]["middles"]["configured"] is False


class TestCliDispatch:
    def test_no_webhooks_configured_exits_nonzero(self, capsys):
        with mock.patch("src.production_config.load_config", return_value=_FakeConfig()):
            exit_code = main(["test-webhooks"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "no discord webhooks configured" in out.lower()

    def test_all_pass_exits_zero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery.send_webhook_message", return_value=True):
            exit_code = main(["test-webhooks"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "PASS" in out

    def test_a_failure_exits_nonzero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery.send_webhook_message", return_value=False):
            exit_code = main(["test-webhooks"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "FAIL" in out


def _fake_diag(success, http_status=None, response_body=None, exception_type=None,
                exception_message=None, timeout=10.0):
    return {
        "success": success, "http_status": http_status, "response_body": response_body,
        "exception_type": exception_type, "exception_message": exception_message, "timeout": timeout,
    }


class TestMlbDiscordConnection:
    """Tests for src.discord_delivery.test_mlb_discord_connection() -- the 2026-09-19 operator
    request for a connectivity check against MLB_DISCORD_WEBHOOKS
    specifically, separate from the general test_webhooks() above."""

    def test_not_configured_reports_zero_urls(self):
        result = run_mlb_discord_test(config=_FakeConfig())
        assert result == {"configured": False, "urls_tested": 0, "passed": 0, "failed": 0, "attempts": []}

    def test_single_url_success(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        diag = _fake_diag(True, http_status=204)
        with mock.patch("src.discord_delivery._send_webhook_diagnostic", return_value=diag) as mocked:
            result = run_mlb_discord_test(config=config)
        assert result["configured"] is True
        assert result["passed"] == 1 and result["failed"] == 0
        assert result["attempts"] == [{"webhook_index": 1, **diag}]
        mocked.assert_called_once_with(
            "https://discord.com/api/webhooks/ev1", "MLB Discord connection test", timeout=10.0,
        )

    def test_multiple_urls_mixed_results_indexed_from_one(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1,https://discord.com/api/webhooks/ev2"
        diag_ok = _fake_diag(True, http_status=204)
        diag_fail = _fake_diag(False, http_status=403, response_body='{"message": "Forbidden"}')
        with mock.patch("src.discord_delivery._send_webhook_diagnostic", side_effect=[diag_ok, diag_fail]):
            result = run_mlb_discord_test(config=config)
        assert result["passed"] == 1 and result["failed"] == 1
        assert [a["webhook_index"] for a in result["attempts"]] == [1, 2]
        assert result["attempts"][1]["http_status"] == 403

    def test_captures_full_diagnostic_detail_on_failure(self):
        """The whole point of this: an HTTP status, response body, and
        exception type/message must all be visible in the job result, not
        just logged where only Render's own log viewer could see it."""
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        diag = _fake_diag(False, http_status=401, response_body='{"message": "401: Unauthorized"}')
        with mock.patch("src.discord_delivery._send_webhook_diagnostic", return_value=diag):
            result = run_mlb_discord_test(config=config)
        attempt = result["attempts"][0]
        assert attempt["http_status"] == 401
        assert attempt["response_body"] == '{"message": "401: Unauthorized"}'
        assert result["failed"] == 1

    def test_captures_exception_detail_when_no_http_response(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        diag = _fake_diag(False, exception_type="URLError", exception_message="<urlopen error timed out>")
        with mock.patch("src.discord_delivery._send_webhook_diagnostic", return_value=diag):
            result = run_mlb_discord_test(config=config)
        attempt = result["attempts"][0]
        assert attempt["http_status"] is None
        assert attempt["exception_type"] == "URLError"
        assert attempt["exception_message"] == "<urlopen error timed out>"

    def test_never_returns_or_logs_the_webhook_url(self, caplog):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1/secrettoken"
        diag = _fake_diag(True, http_status=204)
        with mock.patch("src.discord_delivery._send_webhook_diagnostic", return_value=diag):
            result = run_mlb_discord_test(config=config)
        assert "secrettoken" not in str(result)
        for record in caplog.records:
            assert "secrettoken" not in record.getMessage()
            assert "https://discord.com/api/webhooks" not in record.getMessage()

    def test_ignores_other_channels_arb_and_middle(self):
        """Only MLB_DISCORD_WEBHOOKS (discord_webhook_urls) matters here --
        this is the whole point of a test that answers one narrow
        question, not a general all-channels check like test_webhooks()."""
        config = _FakeConfig()
        config.discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb1"
        config.discord_webhook_urls_middle = "https://discord.com/api/webhooks/mid1"
        result = run_mlb_discord_test(config=config)
        assert result["configured"] is False

    def test_loads_real_production_config_when_none_given(self):
        with mock.patch("src.production_config.load_config", return_value=_FakeConfig()) as mocked_load:
            result = run_mlb_discord_test()
        mocked_load.assert_called_once()
        assert result["configured"] is False


class TestSendWebhookDiagnostic:
    """Tests for _send_webhook_diagnostic() -- the raw, single-attempt,
    no-retry POST helper backing test_mlb_discord_connection(). Its
    retry/rate-limit BEHAVIOR is independent from
    send_webhook_message()/_send_webhook_raw() (used by every real
    delivery path) -- but both share the same DISCORD_REQUEST_HEADERS,
    including the User-Agent fix below."""

    def test_204_is_success(self):
        from src.discord_delivery import _send_webhook_diagnostic

        class _FakeResp:
            def getcode(self):
                return 204
            def read(self):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_FakeResp()):
            result = _send_webhook_diagnostic("https://discord.com/api/webhooks/x", "hi")
        assert result["success"] is True
        assert result["http_status"] == 204

    def test_200_is_success(self):
        from src.discord_delivery import _send_webhook_diagnostic

        class _FakeResp:
            def getcode(self):
                return 200
            def read(self):
                return b'{"ok": true}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_FakeResp()):
            result = _send_webhook_diagnostic("https://discord.com/api/webhooks/x", "hi")
        assert result["success"] is True
        assert result["http_status"] == 200

    def test_http_error_captures_status_and_body(self):
        import io
        from src.discord_delivery import _send_webhook_diagnostic

        exc = urllib.error.HTTPError(
            "https://discord.com/api/webhooks/x", 403, "Forbidden",
            {}, io.BytesIO(b'{"message": "403: Forbidden", "code": 0}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            result = _send_webhook_diagnostic("https://discord.com/api/webhooks/x", "hi")
        assert result["success"] is False
        assert result["http_status"] == 403
        assert "403: Forbidden" in result["response_body"]
        assert result["exception_type"] == "HTTPError"

    def test_network_error_captures_exception_no_http_status(self):
        from src.discord_delivery import _send_webhook_diagnostic

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            result = _send_webhook_diagnostic("https://discord.com/api/webhooks/x", "hi")
        assert result["success"] is False
        assert result["http_status"] is None
        assert result["exception_type"] == "URLError"
        assert "timed out" in result["exception_message"]

    def test_passes_through_the_given_timeout(self):
        from src.discord_delivery import _send_webhook_diagnostic

        class _FakeResp:
            def getcode(self):
                return 204
            def read(self):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_FakeResp()) as mocked:
            result = _send_webhook_diagnostic("https://discord.com/api/webhooks/x", "hi", timeout=3.5)
        assert result["timeout"] == 3.5
        assert mocked.call_args.kwargs["timeout"] == 3.5

    def test_redacts_webhook_url_from_response_body_and_exception_message(self):
        from src.discord_delivery import _send_webhook_diagnostic

        leaky_url = "https://discord.com/api/webhooks/1234/secrettoken"

        class _FakeResp:
            def getcode(self):
                return 400
            def read(self):
                return f'{{"error": "bad url {leaky_url}"}}'.encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_FakeResp()):
            result = _send_webhook_diagnostic(leaky_url, "hi")
        assert "secrettoken" not in result["response_body"]
        assert "[REDACTED_WEBHOOK_URL]" in result["response_body"]

    def test_sends_a_browser_like_user_agent_not_the_urllib_default(self):
        """Root cause of every EV-pick alert failing to ever deliver
        (found via live production testing 2026-09-19): Cloudflare's edge
        (in front of Discord's webhook endpoint) returns HTTP 403 with
        body "error code: 1010" for Python's default
        "Python-urllib/3.x" User-Agent -- a known Cloudflare bot
        signature. A browser-like User-Agent must be sent instead."""
        from src.discord_delivery import _send_webhook_diagnostic

        class _FakeResp:
            def getcode(self):
                return 204
            def read(self):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_FakeResp()) as mocked:
            _send_webhook_diagnostic("https://discord.com/api/webhooks/x", "hi")
        sent_request = mocked.call_args[0][0]
        user_agent = sent_request.get_header("User-agent")
        assert user_agent is not None
        assert "python-urllib" not in user_agent.lower()
        assert "mozilla" in user_agent.lower()


class TestSendWebhookRawUserAgent:
    """_send_webhook_raw() (the real delivery path, via
    send_webhook_message()) must carry the same fix -- the diagnostic
    path alone clearing Cloudflare wouldn't mean anything for actual
    EV-pick delivery, which goes through this function instead."""

    def test_send_webhook_message_sends_a_browser_like_user_agent(self):
        from src.discord_delivery import send_webhook_message

        class _FakeResp:
            def getcode(self):
                return 204
            def read(self):
                return b""
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_FakeResp()) as mocked:
            ok = send_webhook_message("https://discord.com/api/webhooks/x", "hi")
        assert ok is True
        sent_request = mocked.call_args[0][0]
        user_agent = sent_request.get_header("User-agent")
        assert user_agent is not None
        assert "python-urllib" not in user_agent.lower()
        assert "mozilla" in user_agent.lower()


class TestMlbDiscordConnectionCliDispatch:
    def test_not_configured_prints_clear_message_and_exits_nonzero(self, capsys):
        with mock.patch("src.production_config.load_config", return_value=_FakeConfig()):
            exit_code = main(["test-mlb"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "MLB_DISCORD_WEBHOOKS is not set (empty)" in out

    def test_all_pass_prints_pass_and_exits_zero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        diag = _fake_diag(True, http_status=204, response_body="")
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery._send_webhook_diagnostic", return_value=diag):
            exit_code = main(["test-mlb"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "PASS" in out
        assert "1/1" in out
        assert "http_status=204" in out

    def test_a_failure_prints_fail_and_exits_nonzero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        diag = _fake_diag(False, http_status=403, response_body='{"message": "403: Forbidden"}')
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery._send_webhook_diagnostic", return_value=diag):
            exit_code = main(["test-mlb"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "FAIL" in out
        assert "http_status=403" in out
        assert "403: Forbidden" in out
