"""Discord sender failure behaviour (launch audit): 204 accepted, permanent
errors fail fast, 429 backs off then succeeds, timeouts retry a bounded number
of times, nothing raises, and a webhook URL never reaches a log line."""

from __future__ import annotations

import io
import logging
import urllib.error
from unittest import mock

import src.discord_delivery as dd

URL = "https://discord.com/api/webhooks/123456789/AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


class _Resp:
    def __init__(self, status, body=b""):
        self._status, self._body = status, body

    def getcode(self):
        return self._status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError(URL, code, "err", {}, io.BytesIO(body))


def _send(side_effect):
    with mock.patch.object(dd.urllib.request, "urlopen", side_effect=side_effect) as urlopen, \
         mock.patch.object(dd.time, "sleep") as sleep:
        result = dd._send_webhook_raw(URL, {"content": "x"})
    return result, urlopen.call_count, sleep


def test_204_is_success_not_failure():
    ok, calls, _ = _send([_Resp(204)])
    assert ok is True and calls == 1


def test_200_is_accepted_too():
    assert _send([_Resp(200)])[0] is True


def test_403_and_404_fail_fast_without_retrying():
    for code in (400, 401, 403, 404):
        ok, calls, _ = _send([_http_error(code)] * 3)
        assert ok is False and calls == 1, code


def test_429_backs_off_and_then_succeeds():
    ok, calls, sleep = _send([_http_error(429, b'{"retry_after": 0.5}'), _Resp(204)])
    assert ok is True and calls == 2
    assert any(c.args and c.args[0] == 0.5 for c in sleep.call_args_list)


def test_persistent_429_gives_up_after_the_bounded_attempts():
    ok, calls, _ = _send([_http_error(429)] * 10)
    assert ok is False and calls == dd.MAX_RETRIES


def test_server_error_is_retried_a_bounded_number_of_times():
    ok, calls, _ = _send([_http_error(500)] * 10)
    assert ok is False and calls == dd.MAX_RETRIES


def test_timeouts_are_retried_then_fail_without_raising():
    ok, calls, _ = _send([urllib.error.URLError("timed out")] * 10)
    assert ok is False and calls == dd.MAX_RETRIES


def test_a_transient_failure_then_success_recovers():
    ok, calls, _ = _send([urllib.error.URLError("boom"), _Resp(204)])
    assert ok is True and calls == 2


def test_an_unexpected_exception_returns_false_never_raises():
    ok, calls, _ = _send([RuntimeError("weird")])
    assert ok is False and calls == 1


def test_the_webhook_url_never_appears_in_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        _send([_http_error(403)])
        _send([urllib.error.URLError("nope")] * 3)
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" not in caplog.text
    assert "webhooks/123456789" not in caplog.text
