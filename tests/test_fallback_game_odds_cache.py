"""Tests for the shared whole-sport fallback game-odds fetch (2026-10-17).

Production evidence (2026-10-01..17): with SportsGameOdds returning 429, every MLB/NFL/CFB pipeline run asked The Odds
API for the identical whole-sport slate TWICE, about one second apart (daily_pipeline._stage_fetch_events and
player_prop_scanner.run_scan), and paid for both because the request window was built from now() to the second and the
client had no cache lifetime. These tests use fake HTTP responses and temporary cache/database files only.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

from src import odds_api_client as oc
from src.odds_api_client import (
    FALLBACK_GAME_ODDS_TTL_SECONDS, FALLBACK_WINDOW_BUCKET_SECONDS, OddsAPIClient, TRACKED_BOOKMAKERS,
    fallback_game_odds_window, fetch_fallback_game_odds,
)

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _game(game_id="game-1", home="New York Yankees", away="Toronto Blue Jays", commence="2026-10-17T23:36:00Z"):
    def book(key, a, h, over, under):
        return {"key": key, "last_update": "2026-10-17T17:00:00Z", "markets": [
            {"key": "h2h", "outcomes": [{"name": away, "price": a}, {"name": home, "price": h}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": over, "point": 8.5},
                                            {"name": "Under", "price": under, "point": 8.5}]},
        ]}
    return {"id": game_id, "home_team": home, "away_team": away, "commence_time": commence,
            "bookmakers": [book("fanduel", 130, -150, -110, -110), book("draftkings", 125, -145, -108, -112)]}


class FakeHttp:
    """Replaces OddsAPIClient._request_with_retry: counts REAL provider requests and records their params."""

    def __init__(self, games=None, cost="6"):
        self.calls = []
        self.games = [_game()] if games is None else games
        self.cost = cost
        self.lock = threading.Lock()

    def __call__(self, client_self, url, params, **_kw):
        with self.lock:
            self.calls.append((url, dict(params)))
            n = len(self.calls)
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {"x-requests-remaining": str(7000 - 6 * n), "x-requests-used": str(13000 + 6 * n),
                        "x-requests-last": self.cost}
        resp.raise_for_status.return_value = None
        resp.json.return_value = self.games
        return resp


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("THE_ODDS_API_KEY", "dummy-test-key")
    monkeypatch.setattr(oc, "FALLBACK_GAME_ODDS_CACHE_DIR", str(tmp_path / "cache"))
    fake = FakeHttp()
    monkeypatch.setattr(OddsAPIClient, "_request_with_retry", lambda self, url, params, **kw: fake(self, url, params))
    monkeypatch.setattr(OddsAPIClient, "MIN_API_INTERVAL", 0.0)
    return fake


def _age_cache_files(tmp_path, seconds):
    for f in (tmp_path / "cache").glob("*.json"):
        t = f.stat().st_mtime - seconds
        os.utime(f, (t, t))


# ---------------------------------------------------------------------------------------------------------------
# window normalisation
# ---------------------------------------------------------------------------------------------------------------
class TestWindowNormalisation:
    @pytest.mark.parametrize("now", [
        datetime(2026, 10, 17, 19, 3, 35, tzinfo=timezone.utc),
        datetime(2026, 10, 17, 19, 5, 0, tzinfo=timezone.utc),       # exactly on a bucket boundary
        datetime(2026, 10, 17, 19, 4, 59, 999000, tzinfo=timezone.utc),
        datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc),     # year rollover
        datetime(2026, 10, 17, 15, 3, 35, tzinfo=timezone(timedelta(hours=-4))),  # non-UTC input
    ])
    def test_window_is_a_superset_of_the_old_exact_window(self, now):
        lo, hi = fallback_game_odds_window(now)
        lo_dt = datetime.strptime(lo, FMT).replace(tzinfo=timezone.utc)
        hi_dt = datetime.strptime(hi, FMT).replace(tzinfo=timezone.utc)
        assert lo_dt <= now - timedelta(hours=6)
        assert hi_dt >= now + timedelta(hours=42)
        # ...but never wider than one bucket on either side
        assert (now - timedelta(hours=6)) - lo_dt < timedelta(seconds=FALLBACK_WINDOW_BUCKET_SECONDS)
        assert hi_dt - (now + timedelta(hours=42)) <= timedelta(seconds=FALLBACK_WINDOW_BUCKET_SECONDS)

    def test_one_second_apart_inside_a_bucket_is_the_same_window(self):
        a = datetime(2026, 10, 17, 19, 3, 35, tzinfo=timezone.utc)
        assert fallback_game_odds_window(a) == fallback_game_odds_window(a + timedelta(seconds=1.0))
        assert fallback_game_odds_window(a) == fallback_game_odds_window(a + timedelta(seconds=60))

    def test_a_later_bucket_is_a_different_window(self):
        a = datetime(2026, 10, 17, 19, 3, 35, tzinfo=timezone.utc)
        b = a + timedelta(seconds=FALLBACK_WINDOW_BUCKET_SECONDS)
        assert fallback_game_odds_window(a) != fallback_game_odds_window(b)

    def test_format_matches_the_api_and_the_old_callers(self):
        lo, hi = fallback_game_odds_window(datetime(2026, 10, 17, 19, 3, 35, tzinfo=timezone.utc))
        assert (lo, hi) == ("2026-10-17T13:00:00Z", "2026-10-19T13:05:00Z")
        datetime.strptime(lo, FMT), datetime.strptime(hi, FMT)

    def test_default_now_is_used(self):
        lo, hi = fallback_game_odds_window()
        assert lo < hi


# ---------------------------------------------------------------------------------------------------------------
# the cache itself (real OddsAPIClient, fake HTTP)
# ---------------------------------------------------------------------------------------------------------------
NOW = datetime(2026, 10, 17, 19, 3, 35, tzinfo=timezone.utc)


class TestSharedFetch:
    def test_second_identical_request_within_the_ttl_is_a_cache_hit(self, http):
        g1, hit1, _ = fetch_fallback_game_odds("americanfootball_ncaaf", now=NOW)
        g2, hit2, _ = fetch_fallback_game_odds("americanfootball_ncaaf", now=NOW + timedelta(seconds=1))
        assert len(http.calls) == 1
        assert (hit1, hit2) == (False, True)
        assert g1 == g2 and len(g2) == 1

    def test_request_shape_is_unchanged_apart_from_the_bucketed_window(self, http):
        fetch_fallback_game_odds("baseball_mlb", now=NOW)
        url, params = http.calls[0]
        assert url.endswith("/sports/baseball_mlb/odds")
        assert params["bookmakers"] == TRACKED_BOOKMAKERS and "regions" not in params
        assert params["markets"] == "h2h,spreads,totals"
        assert (params["commenceTimeFrom"], params["commenceTimeTo"]) == fallback_game_odds_window(NOW)
        assert params["oddsFormat"] == "american"

    def test_expired_entry_is_refetched(self, http, tmp_path):
        fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        _age_cache_files(tmp_path, FALLBACK_GAME_ODDS_TTL_SECONDS + 5)
        _, hit, _ = fetch_fallback_game_odds("americanfootball_nfl", now=NOW + timedelta(seconds=130))
        assert len(http.calls) == 2 and hit is False

    def test_entry_just_inside_the_ttl_is_still_reused(self, http, tmp_path):
        fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        _age_cache_files(tmp_path, FALLBACK_GAME_ODDS_TTL_SECONDS - 20)
        _, hit, _ = fetch_fallback_game_odds("americanfootball_nfl", now=NOW + timedelta(seconds=20))
        assert len(http.calls) == 1 and hit is True

    def test_the_cache_lifetime_is_short_and_explicit(self):
        assert FALLBACK_GAME_ODDS_TTL_SECONDS == 120

    def test_default_client_is_built_with_the_explicit_ttl(self, http):
        _, _, client = fetch_fallback_game_odds("baseball_mlb", now=NOW)
        assert client.max_cache_age == FALLBACK_GAME_ODDS_TTL_SECONDS

    def test_different_sport_is_a_different_request(self, http):
        fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        fetch_fallback_game_odds("americanfootball_ncaaf", now=NOW)
        fetch_fallback_game_odds("baseball_mlb", now=NOW)
        assert sorted(c[0].rsplit("/", 2)[-2] for c in http.calls) == [
            "americanfootball_ncaaf", "americanfootball_nfl", "baseball_mlb"]

    def test_different_window_bucket_is_a_different_request(self, http):
        fetch_fallback_game_odds("baseball_mlb", now=NOW)
        fetch_fallback_game_odds("baseball_mlb", now=NOW + timedelta(seconds=FALLBACK_WINDOW_BUCKET_SECONDS))
        assert len(http.calls) == 2

    def test_different_books_markets_or_format_never_share_an_entry(self, http, tmp_path):
        client = OddsAPIClient(cache_dir=tmp_path / "cache", max_cache_age=FALLBACK_GAME_ODDS_TTL_SECONDS)
        lo, hi = fallback_game_odds_window(NOW)
        base = dict(sport_key="baseball_mlb", commence_time_from=lo, commence_time_to=hi)
        client.get_odds(bookmakers=TRACKED_BOOKMAKERS, markets="h2h,spreads,totals", **base)
        client.get_odds(bookmakers=TRACKED_BOOKMAKERS, markets="h2h,spreads,totals", **base)   # hit
        assert len(http.calls) == 1
        client.get_odds(bookmakers=TRACKED_BOOKMAKERS, markets="h2h", **base)                   # other markets
        client.get_odds(bookmakers="pinnacle", markets="h2h,spreads,totals", **base)            # other books
        client.get_odds(bookmakers="draftkings,fanduel", markets="h2h,spreads,totals", **base)  # subset of books
        client.get_odds(bookmakers=TRACKED_BOOKMAKERS, markets="h2h,spreads,totals", odds_format="decimal", **base)
        assert len(http.calls) == 5

    def test_event_filter_is_applied_after_the_shared_fetch(self, http):
        """Per-game callers filter the whole-sport response themselves, so they can share the cached entry."""
        http.games = [_game("a"), _game("b", "Boston Red Sox", "Chicago Cubs")]
        g, _, _ = fetch_fallback_game_odds("baseball_mlb", now=NOW)
        g2, hit, _ = fetch_fallback_game_odds("baseball_mlb", now=NOW)
        assert hit and [x["id"] for x in g2] == ["a", "b"] and len(http.calls) == 1

    def test_http_error_is_not_cached(self, http, monkeypatch):
        def boom(self, url, params, **_kw):
            resp = mock.MagicMock()
            resp.status_code = 500
            resp.headers = {}
            resp.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
            return resp
        monkeypatch.setattr(OddsAPIClient, "_request_with_retry", boom)
        with pytest.raises(requests.exceptions.HTTPError):
            fetch_fallback_game_odds("baseball_mlb", now=NOW)
        monkeypatch.setattr(OddsAPIClient, "_request_with_retry", lambda self, url, params, **kw: http(self, url, params))
        _, hit, _ = fetch_fallback_game_odds("baseball_mlb", now=NOW)
        assert hit is False and len(http.calls) == 1


class TestCacheKeyIsNotTruncated:
    """The cache file name used to be cut at 200 characters, which with the 12-book roster dropped markets,
    oddsFormat and the window end from the key (materially different requests could collide)."""

    def test_long_keys_stay_distinct_per_parameter(self, tmp_path, monkeypatch):
        monkeypatch.setenv("THE_ODDS_API_KEY", "dummy-test-key")
        c = OddsAPIClient(cache_dir=tmp_path)
        base = {"bookmakers": TRACKED_BOOKMAKERS, "commenceTimeFrom": "2026-10-17T13:00:00Z",
                "commenceTimeTo": "2026-10-19T13:05:00Z", "includeLinks": "true", "markets": "h2h,spreads,totals",
                "oddsFormat": "american"}
        ep = "/sports/baseball_mlb/odds"
        names = {c._cache_path(ep, base).name}
        for k, v in (("markets", "h2h"), ("oddsFormat", "decimal"), ("commenceTimeTo", "2026-10-19T13:10:00Z"),
                     ("commenceTimeFrom", "2026-10-17T13:05:00Z"), ("bookmakers", TRACKED_BOOKMAKERS + ",bovada")):
            names.add(c._cache_path(ep, {**base, k: v}).name)
        names.add(c._cache_path("/sports/baseball_mlb/events/abc/odds", base).name)
        assert len(names) == 7
        assert all(len(n) < 200 for n in names)

    def test_identical_requests_still_map_to_the_same_file_and_short_keys_are_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("THE_ODDS_API_KEY", "dummy-test-key")
        c = OddsAPIClient(cache_dir=tmp_path)
        p = {"markets": "h2h", "bookmakers": "pinnacle"}
        assert c._cache_path("/sports/x/odds", p) == c._cache_path("/sports/x/odds", dict(p))
        assert c._cache_path("/sports/x/odds", p).name == "_sports_x_odds_bookmakers_pinnacle_markets_h2h.json"


class TestConcurrencyAndCacheFileSafety:
    def test_simultaneous_callers_make_one_request(self, http):
        results, errors = [], []

        def worker():
            try:
                results.append(fetch_fallback_game_odds("americanfootball_nfl", now=NOW))
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert len(http.calls) == 1
        assert sorted(r[1] for r in results) == [False] + [True] * 7

    def test_a_corrupt_or_half_written_cache_file_is_a_miss_not_an_error(self, http, tmp_path):
        fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        for f in (tmp_path / "cache").glob("*.json"):
            f.write_text('[{"id": "trunc', encoding="utf-8")
        games, hit, _ = fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        assert hit is False and len(games) == 1 and len(http.calls) == 2
        # ...and the rewritten file is valid again
        _, hit, _ = fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        assert hit is True

    def test_cache_writes_are_atomic_and_leave_no_temp_files(self, http, tmp_path):
        fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        files = list((tmp_path / "cache").iterdir())
        assert files and all(f.suffix == ".json" for f in files)
        json.loads(files[0].read_text(encoding="utf-8"))

    def test_replace_failure_falls_back_to_a_direct_write(self, http, tmp_path):
        with mock.patch.object(oc.os, "replace", side_effect=PermissionError("in use")):
            fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        _, hit, _ = fetch_fallback_game_odds("americanfootball_nfl", now=NOW)
        assert hit is True
        assert not list((tmp_path / "cache").glob("*.tmp"))


# ---------------------------------------------------------------------------------------------------------------
# league fallbacks: the two pipeline stages, quota accounting, game saving
# ---------------------------------------------------------------------------------------------------------------
def _league_fns():
    from src.sports import cfb, mlb, nfl
    return [("MLB", mlb.fetch_game_odds_via_odds_api, "mlb_fallback_game_odds"),
            ("NFL", nfl.fetch_game_odds_via_odds_api, "nfl_fallback_game_odds"),
            ("NCAAF", cfb.fetch_game_odds_via_odds_api, "cfb_fallback_game_odds")]


@pytest.mark.parametrize("league,fn,job_type", _league_fns())
class TestLeagueFallbackSharesOneRequest:
    def test_two_calls_in_one_run_make_one_provider_request(self, http, db_conn, league, fn, job_type):
        # stage 3 (_stage_fetch_events) then the scanner (run_scan) both call the same fallback function
        odds1, audit1, events1, hit1 = fn(event_id=None, conn=db_conn)
        odds2, audit2, events2, hit2 = fn(event_id=None, conn=db_conn)
        assert len(http.calls) == 1
        # a hit is at most 120 s old, i.e. live data: neither call may be reported as stale cache (the scanner turns
        # a True here into data_source="CACHE" / research_only); the quota row below still records the real hit
        assert (hit1, hit2) == (False, False)
        assert events1 and events1 == events2             # stage 3 gets the events it saves as games
        strip = lambda rows: [{k: v for k, v in r.items() if k != "captured_at"} for r in rows]   # parse-time stamp
        assert odds2 and strip(odds1) == strip(odds2)     # the scanner gets the odds it needs
        assert events2[0]["teams"]["home"]["name"] == "New York Yankees"

    def test_quota_accounting_distinguishes_real_requests_from_cache_hits(self, http, db_conn, league, fn, job_type):
        fn(event_id=None, conn=db_conn)
        fn(event_id=None, conn=db_conn)
        rows = db_conn.execute(
            "SELECT endpoint, job_type, requests_used, requests_remaining, requests_last, cache_hit "
            "FROM odds_api_credits ORDER BY id").fetchall()
        assert [r["job_type"] for r in rows] == [job_type, job_type]
        real, hit = rows
        assert real["cache_hit"] == 0 and real["requests_last"] == 6 and real["requests_remaining"] == 6994
        assert hit["cache_hit"] == 1
        assert hit["requests_last"] is None and hit["requests_used"] is None and hit["requests_remaining"] is None
        # spend is the sum over real requests only -- nothing is invented for the cache hit
        from src.odds_api_credits import get_usage_this_month, get_latest_credit_status
        assert get_usage_this_month(db_conn)["credits_used_so_far"] == 6
        assert get_latest_credit_status(db_conn)["requests_remaining"] == 6994

    def test_event_scoped_call_shares_the_whole_sport_response_and_filters(self, http, db_conn, league, fn, job_type):
        http.games = [_game("a"), _game("b", "Boston Red Sox", "Chicago Cubs")]
        _, _, all_events, _ = fn(event_id=None, conn=db_conn)
        _, _, one, hit = fn(event_id="b", conn=db_conn)
        assert len(http.calls) == 1 and hit is False        # served from the shared cache, reported live
        assert [e["id"] for e in all_events] == ["a", "b"] and [e["id"] for e in one] == ["b"]

    def test_budget_gate_still_runs_before_any_fetch_and_is_unchanged(self, http, db_conn, league, fn, job_type):
        from src.odds_api_credits import record_credit_usage
        record_credit_usage(db_conn, endpoint="odds", requests_used=19995, requests_remaining=5)
        with pytest.raises(RuntimeError, match="budget"):
            fn(event_id=None, conn=db_conn)
        assert http.calls == []

    def test_leagues_do_not_share_entries(self, http, db_conn, league, fn, job_type):
        for _, other_fn, _ in _league_fns():
            other_fn(event_id=None, conn=db_conn)
        assert len(http.calls) == 3
        sports = sorted(c[0].rsplit("/", 2)[-2] for c in http.calls)
        assert sports == ["americanfootball_ncaaf", "americanfootball_nfl", "baseball_mlb"]


class TestPipelineStageThenScannerEndToEnd:
    """The real _stage_fetch_events 429 path followed by the scanner's call: one request, games saved by stage 3."""

    def test_stage3_saves_games_and_the_scanner_call_reuses_the_response(self, http, tmp_path):
        from database.db_manager import init_db, get_connection
        from src import daily_pipeline as dp
        from src.sports import nfl

        db_path = tmp_path / "pipe.db"
        init_db(str(db_path))
        saved = []

        def new_conn(*_a, **_k):
            return get_connection(str(db_path))

        resp = mock.MagicMock()
        resp.status_code = 429
        err = requests.exceptions.HTTPError("429")
        err.response = resp
        config = dp.PipelineConfig(dry_run=False, live=True, output_dir=str(tmp_path / "out"), league="NFL")
        state = dp.PipelineState()

        with mock.patch("src.daily_pipeline.SportsGameOddsClient") as sgo, \
             mock.patch("src.daily_pipeline.get_connection", side_effect=new_conn), \
             mock.patch("src.daily_pipeline.save_game", side_effect=lambda c, g: saved.append(g)), \
             mock.patch.dict(os.environ, {"SPORTSODDS_API_KEY": "test_key"}):
            sgo.return_value.get_events.side_effect = err
            ok = dp._stage_fetch_events(config, state)
            assert ok is True
            # ...then run_scan's own fallback call, exactly as player_prop_scanner does it
            conn = get_connection(str(db_path))
            try:
                odds, audit, events, from_cache = nfl.fetch_game_odds_via_odds_api(event_id=None, conn=conn)
            finally:
                conn.close()

        assert len(http.calls) == 1
        assert from_cache is False and odds and events
        assert [g["event_id"] for g in saved] == ["game-1"]
        assert saved[0]["league"] == "NFL" and saved[0]["home_team"] == "New York Yankees"
        conn = get_connection(str(db_path))
        try:
            rows = conn.execute("SELECT cache_hit, requests_last FROM odds_api_credits ORDER BY id").fetchall()
        finally:
            conn.close()
        assert [(r["cache_hit"], r["requests_last"]) for r in rows] == [(0, 6), (1, None)]


class TestRunScanDoesNotMislabelAFreshCacheHit:
    """The real scanner path: stage 3 (daily_pipeline) primes the shared cache, then run_scan's own fallback call is a
    cache hit. That hit is live data, so the scan must not be marked research-only or labelled data_source="CACHE"
    (those labels are for stale cached data and feed the market-quality freshness score)."""

    def _run(self, http, tmp_path, monkeypatch, league_module, league):
        import socket
        from datetime import datetime, timedelta, timezone

        from database.db_manager import get_connection, init_db
        from src import player_prop_scanner as scanner
        from src import prop_config as cfg

        future = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        http.games = [_game("scan-1", commence=future)]
        # no Pinnacle sources and no network: the only HTTP in this test is the faked Odds API call
        monkeypatch.setattr(cfg, "ODDS_API_PINNACLE_ENABLED", False)
        monkeypatch.setattr(cfg, "PINNACLE_FEED_ENABLED", False)
        monkeypatch.setattr(socket.socket, "connect", mock.Mock(side_effect=AssertionError("network attempted")))

        db_path = tmp_path / "scan.db"
        init_db(str(db_path))
        http_429 = requests.exceptions.HTTPError("429")
        http_429.response = mock.MagicMock(status_code=429)

        # stage 3 of the pipeline: the first fallback call (real request, primes the cache)
        league_module.fetch_game_odds_via_odds_api(event_id=None, conn=None)
        assert len(http.calls) == 1

        with mock.patch.object(scanner, "SportsGameOddsClient") as sgo, \
             mock.patch.object(scanner, "get_connection", side_effect=lambda *a, **k: get_connection(str(db_path))):
            sgo.return_value.get_events.side_effect = http_429
            result = scanner.run_scan(mode="all", market="all", market_form="all", league=league)
        return result, db_path

    @pytest.mark.parametrize("league", ["NFL", "MLB", "NCAAF"])
    def test_scan_stays_live_when_its_fallback_call_is_a_cache_hit(self, http, tmp_path, monkeypatch, league):
        from src.sports import get_league
        result, db_path = self._run(http, tmp_path, monkeypatch, get_league(league), league)
        assert len(http.calls) == 1                         # the scanner's call really was served from the cache
        assert result["n_approved_rows"] > 0                # odds reached the analysis (not an empty-result path)
        assert result["data_source"] == "LIVE API"          # same label the SportsGameOdds path uses for fresh data
        assert result["research_only"] is False
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            run = conn.execute("SELECT data_source, research_only FROM scan_runs WHERE run_type = 'scan'").fetchone()
            hits = conn.execute(
                f"SELECT cache_hit, requests_last FROM odds_api_credits WHERE job_type = '{league.lower() if league != 'NCAAF' else 'cfb'}_fallback_game_odds'"
            ).fetchall()
        finally:
            conn.close()
        assert (run["data_source"], run["research_only"]) == ("LIVE API", 0)
        # the quota/diagnostic row still records the cache hit truthfully (no provider headers, no credits)
        assert [(h["cache_hit"], h["requests_last"]) for h in hits] == [(1, None)]

    def test_a_genuinely_old_cache_file_is_not_reused_so_the_scan_cannot_be_stale(self, http, tmp_path, monkeypatch):
        from src.sports import nfl
        nfl.fetch_game_odds_via_odds_api(event_id=None, conn=None)
        _age_cache_files(tmp_path, FALLBACK_GAME_ODDS_TTL_SECONDS + 30)
        nfl.fetch_game_odds_via_odds_api(event_id=None, conn=None)
        assert len(http.calls) == 2                         # beyond the lifetime it is refetched, never served stale
