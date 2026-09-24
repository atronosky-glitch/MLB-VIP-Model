"""Safe failure diagnostics: sanitizing, structured detail, and the fetch
stage's use of them (provider, stage, sport, HTTP status, exception class,
sanitized message, retry). No secret or query string may reach logs/state."""

from __future__ import annotations

import logging
from unittest import mock

import requests

from src.failure_diagnostics import build_failure_detail, format_failure, sanitize_message

SECRET = "abcd1234efgh5678ijkl9012mnop3456qrst"


def _http_error(status, url):
    resp = requests.Response()
    resp.status_code = status
    resp.url = url
    return requests.exceptions.HTTPError(f"{status} Client Error: Too Many Requests for url: {url}", response=resp)


class TestSanitize:
    def test_url_query_string_with_api_key_is_dropped(self):
        url = f"https://api.the-odds-api.com/v4/sports/x/odds?apiKey={SECRET}&regions=us"
        out = sanitize_message(f"429 for url: {url}")
        assert SECRET not in out and "regions" not in out
        assert "https://api.the-odds-api.com/v4/sports/x/odds" in out

    def test_key_value_pairs_and_headers_are_redacted(self):
        for text in (f"x-api-key: {SECRET}", f"api_key={SECRET}", f"token={SECRET}", f"Authorization: Bearer {SECRET}"):
            assert SECRET not in sanitize_message(text), text

    def test_bare_long_tokens_are_redacted_and_length_is_bounded(self):
        assert SECRET not in sanitize_message(f"failed with {SECRET}")
        assert len(sanitize_message("x " * 1000)) <= 300

    def test_never_raises_on_odd_input(self):
        class Bad:
            def __str__(self):
                raise RuntimeError
        assert sanitize_message(Bad()) == "<unprintable>"
        assert sanitize_message(None) == "None"


class TestBuildFailureDetail:
    def test_http_error_detail_has_every_required_field(self):
        exc = _http_error(429, f"https://x.test/events?apiKey={SECRET}")
        d = build_failure_detail(exc, provider="sportsgameodds", stage="fetch_events", sport="NFL",
                                 retried=True, attempts=4)
        assert d["provider"] == "sportsgameodds" and d["stage"] == "fetch_events" and d["sport"] == "NFL"
        assert d["http_status"] == 429 and d["exception_class"] == "HTTPError"
        assert d["retried"] is True and d["attempts"] == 4
        assert SECRET not in str(d)
        assert "?" not in d["message"]

    def test_non_http_exception_has_no_status(self):
        d = build_failure_detail(ConnectionError("boom"), provider="p", stage="s", sport=None)
        assert d["http_status"] is None and d["exception_class"] == "ConnectionError"
        assert d["retried"] is None

    def test_format_is_one_greppable_line(self):
        line = format_failure(build_failure_detail(ValueError("x"), provider="p", stage="s", sport="MLB"))
        assert "\n" not in line and "provider=p" in line and "stage=s" in line and "sport=MLB" in line


class TestFetchStageFailureLogging:
    def _run(self, exc, *, fallback_exc=None, caplog=None):
        from src.daily_pipeline import PipelineConfig, PipelineState, _stage_fetch_events

        client = mock.MagicMock()
        client.get_events.side_effect = exc
        client.last_request_stats = {"attempts": 4, "retried": True}
        state = PipelineState()
        config = PipelineConfig(league="NFL", live=True, dry_run=True)
        patches = [mock.patch("src.daily_pipeline.SportsGameOddsClient", return_value=client)]
        if fallback_exc is not None:
            from src import sports
            league = sports.get_league("NFL")
            patches.append(mock.patch.object(league, "fetch_game_odds_via_odds_api", side_effect=fallback_exc))
        for p in patches:
            p.start()
        try:
            ok = _stage_fetch_events(config, state)
        finally:
            for p in patches:
                p.stop()
        return ok, state

    def test_sgo_failure_records_structured_sanitized_detail(self, caplog):
        exc = _http_error(500, f"https://api.sportsgameodds.com/v2/events?apiKey={SECRET}")
        with caplog.at_level(logging.ERROR):
            ok, state = self._run(exc)
        assert ok is False and state.status == "API_FAILURE"
        d = state.failure_detail
        assert (d["provider"], d["stage"], d["sport"], d["http_status"]) == ("sportsgameodds", "fetch_events", "NFL", 500)
        assert d["retried"] is True and d["attempts"] == 4
        assert SECRET not in "".join(state.errors) and SECRET not in caplog.text
        assert "PIPELINE_FAILURE" in caplog.text

    def test_a_failing_odds_api_fallback_is_attributed_to_the_fallback_provider(self):
        ok, state = self._run(_http_error(429, "https://api.sportsgameodds.com/v2/events"),
                              fallback_exc=_http_error(401, f"https://api.the-odds-api.com/v4/x?apiKey={SECRET}"))
        assert ok is False
        assert "the-odds-api" in state.failure_detail["provider"]
        assert state.failure_detail["http_status"] == 401
        assert SECRET not in str(state.failure_detail) and SECRET not in "".join(state.errors)


class TestFailureIsPersistedOnTheRunRow:
    def test_error_message_and_metadata_are_written_and_sanitized(self, tmp_path, monkeypatch):
        import json
        import database.db_manager as dbm
        from src.daily_pipeline import PipelineConfig, PipelineState, _record_failure

        monkeypatch.delenv("DATABASE_URL", raising=False)
        path = str(tmp_path / "run.db")
        dbm.init_db(path)
        conn = dbm.get_connection(path)
        # pre-fix rows were created with an already-dumped string (double-encoded)
        run_id = dbm.create_run(conn, run_type="pipeline", mode="live", market_filter=None,
                                form_filter=None, metadata=json.dumps({"pipeline_run_id": "x"}))
        conn.close()

        exc = _http_error(429, f"https://x.test/v4/odds?apiKey={SECRET}")
        state = PipelineState()
        state.pipeline_run_id = run_id
        state.failure_detail = build_failure_detail(exc, provider="sportsgameodds", stage="fetch_events",
                                                    sport="NFL", retried=True, attempts=4)
        state.errors.append("API fetch failed: HTTPError: " + state.failure_detail["message"])

        with mock.patch("src.daily_pipeline.get_connection", lambda *a, **k: dbm.get_connection(path)):
            _record_failure(PipelineConfig(league="NFL", live=True), state, 3)

        conn = dbm.get_connection(path)
        row = dict(conn.execute("SELECT * FROM scan_runs WHERE run_id = ?", (run_id,)).fetchone())
        conn.close()
        assert row["error_message"].startswith("API fetch failed")
        assert row["finished_at"]
        failure = json.loads(row["metadata_json"])["failure"]
        assert failure["exit_code"] == 3 and failure["http_status"] == 429 and failure["retried"] is True
        assert SECRET not in json.dumps(row, default=str)

    def test_dry_run_persists_nothing_and_never_raises(self):
        from src.daily_pipeline import PipelineConfig, PipelineState, _record_failure
        _record_failure(PipelineConfig(league="NFL", dry_run=True), PipelineState(), 3)
