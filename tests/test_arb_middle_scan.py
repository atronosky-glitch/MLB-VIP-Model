"""Tests for src/arb_middle_scan.py and its worker wiring."""

import inspect
from unittest.mock import patch

import src.worker as worker
from src.arb_middle_scan import run_scan
from database.db_manager import get_active_arbitrage_opportunities, get_active_middle_opportunities


def _insert_odds_row(conn, **overrides):
    row = {
        "event_id": "E1", "odd_id": "odd-1", "sportsbook": "BookA",
        "player_id": "P1", "player_name": "Test Pitcher", "team_id": None, "team_name": None,
        "market_type": "pitching_strikeouts_ou", "market_group_key": "E1|P1|k|6.5",
        "side": "OVER", "line": 6.5, "price": 110, "decimal_odds": 2.10,
        "is_alt_line": 0, "available": 1, "validation_status": "VALID",
        "mapping_confidence": "HIGH", "mapping_method": "exact", "validation_reason": "",
        "captured_at": "2026-09-09T12:00:00+00:00", "league": "MLB",
    }
    row.update(overrides)
    conn.execute(
        """INSERT INTO player_prop_odds
            (event_id, odd_id, sportsbook, player_id, player_name, team_id, team_name,
             market_type, market_group_key, side, line, price, decimal_odds, is_alt_line,
             available, validation_status, mapping_confidence, mapping_method,
             validation_reason, captured_at, league)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (row["event_id"], row["odd_id"], row["sportsbook"], row["player_id"], row["player_name"],
         row["team_id"], row["team_name"], row["market_type"], row["market_group_key"], row["side"],
         row["line"], row["price"], row["decimal_odds"], row["is_alt_line"], row["available"],
         row["validation_status"], row["mapping_confidence"], row["mapping_method"],
         row["validation_reason"], row["captured_at"], row["league"]),
    )
    conn.commit()


class TestRunScan:
    def test_detects_and_persists_a_real_arbitrage(self, db_conn):
        _insert_odds_row(db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10, odd_id="o1")
        _insert_odds_row(db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30, odd_id="o2")

        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)

        assert result["arbitrage"]["detected"] == 1
        active = get_active_arbitrage_opportunities(db_conn, "MLB")
        assert len(active) == 1
        assert active[0]["market_type"] == "pitching_strikeouts_ou"

    def test_excludes_spread_and_runline_markets(self, db_conn):
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="HOME", price=150, decimal_odds=2.50,
            market_type="game_spread_ou", market_group_key="E1|spread|-1.5", line=-1.5, odd_id="o1",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="AWAY", price=150, decimal_odds=2.50,
            market_type="game_spread_ou", market_group_key="E1|spread|-1.5", line=1.5, odd_id="o2",
        )
        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)
        assert result["arbitrage"]["detected"] == 0
        assert get_active_arbitrage_opportunities(db_conn, "MLB") == []

    def test_no_opportunities_is_not_an_error(self, db_conn):
        _insert_odds_row(db_conn, sportsbook="BookA", side="OVER", price=-110, decimal_odds=1.909, odd_id="o1")
        _insert_odds_row(db_conn, sportsbook="BookB", side="UNDER", price=-110, decimal_odds=1.909, odd_id="o2")
        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)
        assert result["arbitrage"]["detected"] == 0
        assert result["middles"]["detected"] == 0

    def test_stale_rows_outside_freshness_window_are_ignored(self, db_conn):
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10,
            captured_at="2020-01-01T00:00:00+00:00", odd_id="o1",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30,
            captured_at="2020-01-01T00:00:00+00:00", odd_id="o2",
        )
        result = run_scan(db_conn, league="MLB", freshness_seconds=3600)
        assert result["rows_examined"] == 0
        assert result["arbitrage"]["detected"] == 0

    def test_first_scan_reports_the_opportunity_as_new(self, db_conn):
        _insert_odds_row(db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10, odd_id="o1")
        _insert_odds_row(db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30, odd_id="o2")

        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)

        assert len(result["new_arbitrage"]) == 1
        assert result["new_arbitrage"][0]["player_name"] == "Test Pitcher"

    def test_rescan_of_the_same_opportunity_is_not_new(self, db_conn):
        _insert_odds_row(db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10, odd_id="o1")
        _insert_odds_row(db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30, odd_id="o2")

        run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)
        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)

        assert result["new_arbitrage"] == []

    def test_opportunities_are_stamped_with_the_real_league_not_left_unset(self, db_conn):
        """Real bug found live in production (2026-09-21): opp["league"]
        was never set on the in-memory opportunity dicts at all (only
        opp["sport"], a coarser value) -- src/message_formatter.py's
        _league_prefix() prefers "league" over "sport" for the Discord
        alert header, so every arbitrage/middle message fell back to
        showing "sport" (or the wrong "baseball" default for NCAAF,
        which was missing from _SPORT_BY_LEAGUE) instead of the real
        league code."""
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10, odd_id="o1", league="NFL",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30, odd_id="o2", league="NFL",
        )
        result = run_scan(db_conn, league="NFL", freshness_seconds=10_000_000)
        assert result["new_arbitrage"][0]["league"] == "NFL"
        assert result["new_middles"] == [] or result["new_middles"][0]["league"] == "NFL"

    def test_ncaaf_sport_fallback_is_football_not_baseball(self, db_conn):
        """_SPORT_BY_LEAGUE was missing an NCAAF entry entirely, so
        .get(league, "baseball") silently mislabeled every college-
        football opportunity's "sport" as baseball. Now moot for the
        Discord message itself (league is stamped and preferred), but
        the sport fallback value should still be correct."""
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10, odd_id="o1", league="NCAAF",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30, odd_id="o2", league="NCAAF",
        )
        result = run_scan(db_conn, league="NCAAF", freshness_seconds=10_000_000)
        assert result["new_arbitrage"][0]["sport"] == "football"
        assert result["new_arbitrage"][0]["league"] == "NCAAF"


class TestEventContext:
    """Real bug found live 2026-09-15: a "Game Total" middle/arbitrage
    opportunity (built directly from raw odds, no model recommendation
    involved) showed matchup=None and no league identification at all
    -- _event_context only ever looked at historical_recommendations,
    which has no row for a game the model never touched. The `games`
    table (populated by the odds/schedule pipeline independent of the
    model) is the fix."""

    def _insert_game(self, conn, event_id="E1", league="NFL", away="Denver Broncos", home="Kansas City Chiefs"):
        conn.execute(
            "INSERT INTO games (event_id, league, away_team, home_team, start_time) VALUES (?, ?, ?, ?, ?)",
            (event_id, league, away, home, "2026-09-15T00:15:00Z"),
        )
        conn.commit()

    def test_matchup_resolved_from_games_table_with_no_recommendation_row(self, db_conn):
        self._insert_game(db_conn)
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10,
            market_type="game_total_ou", player_id="GAME", player_name="Game Total",
            market_group_key="E1|game_total|46.0", line=46.0, odd_id="o1", league="NFL",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30,
            market_type="game_total_ou", player_id="GAME", player_name="Game Total",
            market_group_key="E1|game_total|46.0", line=46.0, odd_id="o2", league="NFL",
        )
        result = run_scan(db_conn, league="NFL", freshness_seconds=10_000_000)
        assert result["arbitrage"]["detected"] == 1
        active = get_active_arbitrage_opportunities(db_conn, "NFL")
        assert len(active) == 1
        assert active[0]["matchup"] == "Denver Broncos @ Kansas City Chiefs"
        assert active[0]["league"] == "NFL"

    def test_no_games_row_and_no_recommendation_leaves_matchup_none_not_a_crash(self, db_conn):
        _insert_odds_row(db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10, odd_id="o1")
        _insert_odds_row(db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30, odd_id="o2")
        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)
        assert result["arbitrage"]["detected"] == 1
        active = get_active_arbitrage_opportunities(db_conn, "MLB")
        assert active[0]["matchup"] is None

    def test_matchup_resolved_even_when_odds_row_is_mistagged_with_the_wrong_league(self, db_conn):
        """Real bug found live in production (2026-09-22): a WNBA game's
        player_prop_odds rows were mistagged league='MLB' at ingestion
        (a data bug, separately fixed at the source -- see
        save_player_prop_batch). Because _event_context used to be
        scoped by the SAME league the odds row was (wrongly) tagged
        with, the MLB-scoped scan pass could never find the real
        (league='WNBA') games row for that event_id -- producing a
        permanent "Matchup unavailable" card for a real, identifiable
        game. _event_context now looks up by event_id alone, so the
        true matchup/league resolve regardless of which league's odds
        bucket the row was mistakenly swept into."""
        self._insert_game(db_conn, event_id="E1", league="WNBA", away="Atlanta Dream", home="New York Liberty")
        # Odds mistagged 'MLB' -- exactly the production data shape.
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10,
            market_type="game_total_ou", player_id="GAME", player_name="Game Total",
            market_group_key="E1|game_total|177.5", line=177.5, odd_id="o1", league="MLB",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="UNDER", price=100, decimal_odds=2.00,
            market_type="game_total_ou", player_id="GAME", player_name="Game Total",
            market_group_key="E1|game_total|177.5", line=177.5, odd_id="o2", league="MLB",
        )
        result = run_scan(db_conn, league="MLB", freshness_seconds=10_000_000)
        assert result["arbitrage"]["detected"] == 1
        # The real-time in-memory opportunity (what src/worker.py's
        # Discord alert is built from) must show the TRUE matchup and
        # league, not "MLB"/unavailable just because that's which scan
        # pass happened to find the mistagged odds.
        assert result["new_arbitrage"][0]["matchup"] == "Atlanta Dream @ New York Liberty"
        assert result["new_arbitrage"][0]["league"] == "WNBA"
        # The persisted row's matchup is corrected too (website display).
        active = get_active_arbitrage_opportunities(db_conn, "MLB")
        assert len(active) == 1
        assert active[0]["matchup"] == "Atlanta Dream @ New York Liberty"

    def test_matchup_self_heals_on_a_later_pass_once_the_games_row_appears(self, db_conn):
        """Documented-but-previously-unimplemented behavior: an
        opportunity detected before the games/schedule sync catches up
        (matchup=None at creation) should pick up the real matchup on a
        later re-sync of the SAME still-active opportunity, once
        games has the row -- the ON CONFLICT UPDATE previously never
        wrote matchup/event_start_time/sport back for an existing row."""
        _insert_odds_row(
            db_conn, sportsbook="BookA", side="OVER", price=110, decimal_odds=2.10,
            market_type="game_total_ou", player_id="GAME", player_name="Game Total",
            market_group_key="E1|game_total|46.0", line=46.0, odd_id="o1", league="NFL",
        )
        _insert_odds_row(
            db_conn, sportsbook="BookB", side="UNDER", price=130, decimal_odds=2.30,
            market_type="game_total_ou", player_id="GAME", player_name="Game Total",
            market_group_key="E1|game_total|46.0", line=46.0, odd_id="o2", league="NFL",
        )
        first = run_scan(db_conn, league="NFL", freshness_seconds=10_000_000)
        assert first["new_arbitrage"][0]["matchup"] is None

        self._insert_game(db_conn, event_id="E1", league="NFL", away="Denver Broncos", home="Kansas City Chiefs")
        run_scan(db_conn, league="NFL", freshness_seconds=10_000_000)

        active = get_active_arbitrage_opportunities(db_conn, "NFL")
        assert len(active) == 1
        assert active[0]["matchup"] == "Denver Broncos @ Kansas City Chiefs"


class TestWorkerWiring:
    def test_arb_middle_scan_registered_in_dispatch(self):
        source = inspect.getsource(worker._execute_job)
        assert '"arb-middle-scan"' in source

    def test_one_league_failing_does_not_block_the_others(self, db_conn):
        calls = []

        def fake_run_scan(conn, league="MLB", **kwargs):
            calls.append(league)
            if league == "NFL":
                raise RuntimeError("boom")
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 0}}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan):
            result = worker._run_arb_middle_scan(db_conn, config=None)

        assert calls == ["MLB", "NFL", "WNBA"]
        assert result["status"] == "success"
        assert result["results"]["MLB"]["arbitrage"]["detected"] == 0
        assert result["results"]["NFL"] == {"error": True}
        assert result["results"]["WNBA"]["arbitrage"]["detected"] == 0

    def test_no_discord_delivery_without_webhooks_configured(self, db_conn):
        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = ""

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 0},
                    "new_arbitrage": [], "new_middles": []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.worker._deliver_new_opportunity_alerts") as mock_deliver:
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        mock_deliver.assert_not_called()

    def test_ev_pick_channel_does_not_receive_arbitrage_or_middle_alerts(self, db_conn):
        """EV picks and arbitrage/middles go to separate Discord channels
        (2026-09-10) -- only discord_webhook_urls_arb_middle configured
        should trigger arb/middle delivery, never a fallback onto the
        EV-only webhook."""
        class FakeConfig:
            discord_webhook_urls = "https://discord.com/api/webhooks/ev-only"
            discord_webhook_urls_arb_middle = ""
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        new_arb = [{"player_name": "Test Pitcher"}]

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 1}, "middles": {"detected": 0},
                    "new_arbitrage": new_arb if league == "MLB" else [],
                    "new_middles": []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_arbitrage_alerts") as mock_arb, \
             patch("src.discord_delivery.deliver_middle_alerts") as mock_mid, \
             patch("src.discord_delivery.deliver_new_recommendation_alerts") as mock_ev:
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        mock_arb.assert_not_called()
        mock_mid.assert_not_called()
        mock_ev.assert_called_once()
        assert mock_ev.call_args[0][1] == ["https://discord.com/api/webhooks/ev-only"]

    def test_arb_middle_channel_does_not_receive_ev_pick_alerts(self, db_conn):
        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb-mid"
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        new_arb = [{"player_name": "Test Pitcher"}]

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 1}, "middles": {"detected": 0},
                    "new_arbitrage": new_arb if league == "MLB" else [],
                    "new_middles": []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_arbitrage_alerts") as mock_arb, \
             patch("src.discord_delivery.deliver_middle_alerts") as mock_mid, \
             patch("src.discord_delivery.deliver_new_recommendation_alerts") as mock_ev:
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        mock_arb.assert_called_once_with(new_arb, ["https://discord.com/api/webhooks/arb-mid"])
        mock_mid.assert_not_called()
        mock_ev.assert_not_called()

    def test_middle_channel_is_independent_of_the_arbitrage_channel(self, db_conn):
        """A third channel just for middles (2026-09-11) -- configuring
        discord_webhook_urls_middle sends middles there and nowhere
        else, even when discord_webhook_urls_arb_middle is also set for
        arbitrage."""
        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb-only"
            discord_webhook_urls_middle = "https://discord.com/api/webhooks/middle-only"
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        new_arb = [{"player_name": "Test Pitcher"}]
        new_mid = [{"player_name": "Test Batter", "verdict": "WORTH_IT"}]

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 1}, "middles": {"detected": 1},
                    "new_arbitrage": new_arb if league == "MLB" else [],
                    "new_middles": new_mid if league == "MLB" else []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_arbitrage_alerts") as mock_arb, \
             patch("src.discord_delivery.deliver_middle_alerts") as mock_mid, \
             patch("src.discord_delivery.deliver_new_recommendation_alerts") as mock_ev:
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        mock_arb.assert_called_once_with(new_arb, ["https://discord.com/api/webhooks/arb-only"])
        mock_mid.assert_called_once_with(new_mid, ["https://discord.com/api/webhooks/middle-only"])
        mock_ev.assert_not_called()

    def test_only_worth_it_middles_are_alerted_not_worth_it_and_unknown_are_filtered(self, db_conn):
        """A middle the model judges NOT_WORTH_IT or UNKNOWN is still
        detected/persisted (visible in the dashboard) but must never
        reach Discord as if it were an actionable alert -- see
        src/middling.py's verdict field."""
        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = ""
            discord_webhook_urls_middle = "https://discord.com/api/webhooks/middle-only"
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        new_mid = [
            {"player_name": "Worth It Batter", "verdict": "WORTH_IT"},
            {"player_name": "Not Worth It Batter", "verdict": "NOT_WORTH_IT"},
            {"player_name": "Unknown Batter", "verdict": "UNKNOWN"},
        ]

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 3},
                    "new_arbitrage": [],
                    "new_middles": new_mid if league == "MLB" else []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_middle_alerts") as mock_mid:
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        mock_mid.assert_called_once()
        delivered = mock_mid.call_args[0][0]
        assert len(delivered) == 1
        assert delivered[0]["player_name"] == "Worth It Batter"

    def test_middles_do_not_alert_when_only_the_arbitrage_channel_is_configured(self, db_conn):
        class FakeConfig:
            discord_webhook_urls = ""
            discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb-only"
            discord_webhook_urls_middle = ""
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 1},
                    "new_arbitrage": [],
                    "new_middles": [{"player_name": "Test Batter"}] if league == "MLB" else []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_arbitrage_alerts") as mock_arb, \
             patch("src.discord_delivery.deliver_middle_alerts") as mock_mid:
            worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        mock_arb.assert_not_called()
        mock_mid.assert_not_called()

    def test_discord_delivery_failure_does_not_fail_the_scan_job(self, db_conn):
        class FakeConfig:
            discord_webhook_urls = "https://discord.com/api/webhooks/test"
            discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb-mid"
            database_path = "unused"
            min_confidence_score = 40.0
            min_ev_pct = 2.0

        def fake_run_scan(conn, league="MLB", **kwargs):
            return {"league": league, "rows_examined": 0,
                    "arbitrage": {"detected": 0}, "middles": {"detected": 0},
                    "new_arbitrage": [{"player_name": "Boom"}] if league == "MLB" else [],
                    "new_middles": []}

        with patch("src.arb_middle_scan.run_scan", side_effect=fake_run_scan), \
             patch("src.discord_delivery.deliver_arbitrage_alerts", side_effect=RuntimeError("boom")), \
             patch("src.discord_delivery.deliver_new_recommendation_alerts"):
            result = worker._run_arb_middle_scan(db_conn, config=FakeConfig())

        assert result["status"] == "success"
