"""Offline unit tests for the SportsGameOdds API client retry/auth handling.

All requests are mocked — no network calls are ever made. These cover the
fail-fast-on-auth-error behaviour: HTTP 401/403 and any HTTP >= 400 whose body
indicates an invalid API key must not be retried, while 429/5xx/timeouts keep
the existing exponential-backoff retry behaviour.
"""

import logging
import os

os.environ.setdefault("SPORTSODDS_API_KEY", "test_api_key_1234567890")

from unittest import mock

import pytest
import requests

from src.api_client import (
    API_KEY,
    _ENV_VAR,
    _mask_key,
    _is_auth_failure,
    _api_key_diagnostic,
    _api_key_error_message,
    APIKeyError,
    SportsGameOddsClient,
)


class _FakeResp:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(self, status_code: int, text: str = "", payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}: {self.text}")

    def json(self):
        return self._payload


@pytest.fixture
def client(tmp_path):
    return SportsGameOddsClient(cache_dir=str(tmp_path / "cache"))


# ── env var name / diagnostics ─────────────────────────────────────

class TestEnvVarAndDiagnostics:
    def test_env_var_name_is_sportsodds(self):
        assert _ENV_VAR == "SPORTSODDS_API_KEY"

    def test_error_message_names_env_var(self):
        msg = _api_key_error_message()
        assert "Invalid SportsGameOdds API key" in msg
        assert _ENV_VAR in msg

    def test_mask_short_key(self):
        masked = _mask_key("abcdefgh")
        assert "ab..." in masked
        assert "abcdefgh" not in masked

    def test_mask_long_key_never_leaks_full(self):
        key = "0123456789abcdef0123456789abcdef"
        masked = _mask_key(key)
        assert masked.startswith("0123")
        assert masked.endswith("ef")
        assert "..." in masked
        assert key not in masked

    def test_mask_empty(self):
        assert _mask_key("") == "<unset>"

    def test_diagnostic_logs_masked_only(self, caplog):
        with caplog.at_level(logging.INFO, logger="src.api_client"):
            logging.getLogger("src.api_client").info("%s", _api_key_diagnostic())
        assert "API key diagnostic" in caplog.text
        assert _ENV_VAR in caplog.text
        assert f"length={len(API_KEY)}" in caplog.text
        assert API_KEY not in caplog.text


# ── auth failure detection ─────────────────────────────────────────

class TestAuthFailureDetection:
    @pytest.mark.parametrize("status,text", [
        (401, ""),
        (403, "Forbidden"),
        (500, "Internal Server Error: Invalid API key"),
        (502, "Invalid API key"),
        (400, "api key is invalid"),
        (401, "Unauthorized"),
        (500, "authentication failed"),
    ])
    def test_detects_auth_failure(self, status, text):
        assert _is_auth_failure(status, text) is True

    @pytest.mark.parametrize("status,text", [
        (200, "Invalid API key mentioned in a 200 body"),
        (429, "Too Many Requests"),
        (500, "Internal Server Error"),
        (502, "Bad Gateway"),
        (503, "Service Unavailable"),
        (504, "Gateway Timeout"),
    ])
    def test_does_not_flag_transient_errors(self, status, text):
        assert _is_auth_failure(status, text) is False


# ── retry behaviour ────────────────────────────────────────────────

class TestRetryBehaviour:
    def test_401_fails_fast_without_retry(self, client):
        with mock.patch.object(client.session, "get",
                               return_value=_FakeResp(401, "Invalid API key")) as mg:
            with mock.patch("src.api_client.time.sleep"):
                with pytest.raises(APIKeyError) as exc_info:
                    client._request_with_retry("https://api.example/x", max_retries=3)
        assert mg.call_count == 1
        assert _ENV_VAR in str(exc_info.value)

    def test_403_fails_fast_without_retry(self, client):
        with mock.patch.object(client.session, "get",
                               return_value=_FakeResp(403, "Forbidden")) as mg:
            with mock.patch("src.api_client.time.sleep"):
                with pytest.raises(APIKeyError):
                    client._request_with_retry("https://api.example/x", max_retries=3)
        assert mg.call_count == 1

    def test_500_with_invalid_api_key_body_fails_fast(self, client):
        # The exact regression: the API reports an auth error as an HTTP 500
        # with body "Internal Server Error: Invalid API key".
        with mock.patch.object(client.session, "get",
                               return_value=_FakeResp(500, "Internal Server Error: Invalid API key")) as mg:
            with mock.patch("src.api_client.time.sleep"):
                with pytest.raises(APIKeyError) as exc_info:
                    client._request_with_retry("https://api.example/x", max_retries=3)
        assert mg.call_count == 1
        assert "Invalid SportsGameOdds API key" in str(exc_info.value)

    def test_transient_500_is_retried(self, client):
        responses = [
            _FakeResp(500, "Internal Server Error"),
            _FakeResp(200, "ok"),
        ]
        with mock.patch.object(client.session, "get", side_effect=responses) as mg:
            with mock.patch("src.api_client.time.sleep"):
                resp = client._request_with_retry("https://api.example/x", max_retries=3)
        assert mg.call_count == 2
        assert resp.status_code == 200

    def test_429_is_retried(self, client):
        responses = [
            _FakeResp(429, "Too Many Requests"),
            _FakeResp(200, "ok"),
        ]
        with mock.patch.object(client.session, "get", side_effect=responses) as mg:
            with mock.patch("src.api_client.time.sleep"):
                resp = client._request_with_retry("https://api.example/x", max_retries=3)
        assert mg.call_count == 2
        assert resp.status_code == 200

    def test_502_is_retried(self, client):
        responses = [
            _FakeResp(502, "Bad Gateway"),
            _FakeResp(200, "ok"),
        ]
        with mock.patch.object(client.session, "get", side_effect=responses) as mg:
            with mock.patch("src.api_client.time.sleep"):
                resp = client._request_with_retry("https://api.example/x", max_retries=3)
        assert mg.call_count == 2

    def test_timeout_retries_then_raises(self, client):
        with mock.patch.object(client.session, "get",
                               side_effect=requests.exceptions.Timeout) as mg:
            with mock.patch("src.api_client.time.sleep"):
                with pytest.raises(requests.exceptions.Timeout):
                    client._request_with_retry("https://api.example/x", max_retries=2)
        assert mg.call_count == 3  # max_retries + 1 attempts
