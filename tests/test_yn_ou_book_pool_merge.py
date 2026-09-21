"""Tests for _merge_equivalent_yn_ou_books (2026-09-21 operator
request): a book's YN "Yes" price and its Over-0.5 price for the same
stat are numerically identical (confirmed live in production -- same
book, same moment, e.g. BetMGM posting -250 on both "Over 0.5
Strikeouts" and "Yes, will record a strikeout"). Without pooling them,
a book that only quotes one representation never contributes to the
other's consensus/fair-price book count, artificially shrinking it.
"""

from __future__ import annotations

from src.player_prop_scanner import _merge_equivalent_yn_ou_books
from src.sports.base import MarketConfig


def _config(market_type_ou="batting_strikeouts_ou", market_type_yn="batting_strikeouts_yn"):
    return MarketConfig(
        cli_name="strikeouts",
        odd_id_stat_prefix="batting_strikeouts",
        market_type_ou=market_type_ou,
        market_type_yn=market_type_yn,
        display_name="Strikeouts",
        short_label="Ks",
        period="game",
    )


def _yn_group(event_id="E1", player_id="P1", market_type="batting_strikeouts_yn", yes=None):
    return {
        "yes": dict(yes or {}), "player_id": player_id, "player_name": "Test Player",
        "event_id": event_id, "market_type": market_type, "observation_times": [],
    }


def _ou_group(line=0.5, over=None, under=None):
    return {
        "over": dict(over or {}), "under": dict(under or {}), "line": line,
        "player_id": "P1", "player_name": "Test Player", "event_id": "E1",
        "market_type": "batting_strikeouts_ou", "observation_times": [],
        "display_sides": {"over": "OVER", "under": "UNDER"}, "side_raw_line": {},
        "mapping_confidence": "HIGH",
    }


def _ou_key(event_id="E1", player_id="P1", market_type="batting_strikeouts_ou", line=0.5):
    from src.player_prop_parser import _build_group_key
    return _build_group_key(event_id, player_id, line, is_alt_line=0, side=None, market_type=market_type)


class TestMergeEquivalentYnOuBooks:
    def test_yn_only_book_merged_into_ou_over(self):
        """The core scenario: DraftKings only posts the YN version --
        it must show up in the OU-0.5 group's "over" pool too."""
        yn_groups = {"yn-key": _yn_group(yes={
            "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
        })}
        ou_key = _ou_key()
        ou_groups = {ou_key: _ou_group(over={
            "betmgm": {"price": -210, "decimal_odds": 1.476, "line": 0.5,
                       "validation_status": "VALID", "display_side": "OVER"},
        })}
        yn_type_map = {"batting_strikeouts_yn": _config()}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)

        over = ou_groups[ou_key]["over"]
        assert "draftkings" in over
        assert over["draftkings"]["price"] == -214
        assert over["draftkings"]["line"] == 0.5
        assert over["draftkings"]["display_side"] == "OVER"
        # The book that was already there natively is untouched.
        assert over["betmgm"]["price"] == -210

    def test_ou_only_book_merged_into_yn_yes(self):
        """Reverse direction: FanDuel only posts the OU-0.5 version --
        it must show up in the YN group's "yes" pool too."""
        yn_groups = {"yn-key": _yn_group(yes={
            "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
        })}
        ou_key = _ou_key()
        ou_groups = {ou_key: _ou_group(over={
            "draftkings": {"price": -214, "decimal_odds": 1.467, "line": 0.5,
                            "validation_status": "VALID", "display_side": "OVER"},
            "fanduel": {"price": -230, "decimal_odds": 1.435, "line": 0.5,
                        "validation_status": "VALID", "display_side": "OVER"},
        })}
        yn_type_map = {"batting_strikeouts_yn": _config()}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)

        yes = yn_groups["yn-key"]["yes"]
        assert "fanduel" in yes
        assert yes["fanduel"]["price"] == -230

    def test_native_prices_are_never_overwritten(self):
        """A book quoting BOTH representations keeps its own OU-side
        price exactly as offered -- never replaced by anything derived
        from the YN side, even if (hypothetically, for this test) they
        differed. No book's opinion is ever double-counted or altered."""
        yn_groups = {"yn-key": _yn_group(yes={
            "betmgm": {"price": -999, "decimal_odds": 1.01, "validation_status": "VALID"},
        })}
        ou_key = _ou_key()
        ou_groups = {ou_key: _ou_group(over={
            "betmgm": {"price": -210, "decimal_odds": 1.476, "line": 0.5,
                       "validation_status": "VALID", "display_side": "OVER"},
        })}
        yn_type_map = {"batting_strikeouts_yn": _config()}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)

        assert ou_groups[ou_key]["over"]["betmgm"]["price"] == -210  # untouched
        assert yn_groups["yn-key"]["yes"]["betmgm"]["price"] == -999  # untouched

    def test_stat_with_no_ou_sibling_is_skipped_not_crashed(self):
        """e.g. pitching_win_yn has no O/U equivalent at all
        (MarketConfig.market_type_ou=None) -- must be a clean no-op."""
        yn_groups = {"yn-key": _yn_group(
            market_type="pitching_win_yn",
            yes={"draftkings": {"price": 150, "decimal_odds": 2.5, "validation_status": "VALID"}},
        )}
        ou_groups: dict = {}
        yn_type_map = {"pitching_win_yn": _config(market_type_ou=None, market_type_yn="pitching_win_yn")}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)  # must not raise
        assert yn_groups["yn-key"]["yes"] == {
            "draftkings": {"price": 150, "decimal_odds": 2.5, "validation_status": "VALID"},
        }

    def test_no_matching_ou_group_at_all_is_a_clean_noop(self):
        """A YN market with no real Over-0.5 group in this scan pass at
        all must never fabricate one."""
        yn_groups = {"yn-key": _yn_group(yes={
            "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
        })}
        ou_groups: dict = {}
        yn_type_map = {"batting_strikeouts_yn": _config()}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)  # must not raise
        assert ou_groups == {}
        assert yn_groups["yn-key"]["yes"] == {
            "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
        }

    def test_yn_type_not_in_map_is_skipped_not_crashed(self):
        """Defensive: a market_type absent from the registry lookup map
        (shouldn't normally happen, but must never crash the scan)."""
        yn_groups = {"yn-key": _yn_group(market_type="unknown_yn", yes={
            "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
        })}
        ou_groups: dict = {}
        yn_type_map: dict = {}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)  # must not raise

    def test_under_side_is_never_touched(self):
        """Only "over"/"yes" are pooled -- analyze_yn_group has no "no"
        price capture in this codebase today (see the function's own
        docstring), so "under" must be left exactly as-is."""
        yn_groups = {"yn-key": _yn_group(yes={
            "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
        })}
        ou_key = _ou_key()
        ou_groups = {ou_key: _ou_group(
            over={"betmgm": {"price": -210, "decimal_odds": 1.476, "line": 0.5,
                              "validation_status": "VALID", "display_side": "OVER"}},
            under={"betmgm": {"price": 170, "decimal_odds": 2.70, "line": 0.5,
                               "validation_status": "VALID", "display_side": "UNDER"}},
        )}
        yn_type_map = {"batting_strikeouts_yn": _config()}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)

        assert ou_groups[ou_key]["under"] == {
            "betmgm": {"price": 170, "decimal_odds": 2.70, "line": 0.5,
                       "validation_status": "VALID", "display_side": "UNDER"},
        }

    def test_other_group_level_fields_are_untouched(self):
        """Surgical merge -- only "over"/"yes" book dicts change, every
        other key on either group stays exactly as it was."""
        yn_groups = {"yn-key": _yn_group(yes={
            "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
        })}
        ou_key = _ou_key()
        ou_groups = {ou_key: _ou_group(over={
            "betmgm": {"price": -210, "decimal_odds": 1.476, "line": 0.5,
                       "validation_status": "VALID", "display_side": "OVER"},
        })}
        yn_type_map = {"batting_strikeouts_yn": _config()}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)

        assert ou_groups[ou_key]["line"] == 0.5
        assert ou_groups[ou_key]["mapping_confidence"] == "HIGH"
        assert ou_groups[ou_key]["display_sides"] == {"over": "OVER", "under": "UNDER"}
        assert yn_groups["yn-key"]["player_name"] == "Test Player"
        assert yn_groups["yn-key"]["market_type"] == "batting_strikeouts_yn"

    def test_two_different_players_merge_independently(self):
        """No cross-contamination between unrelated players' groups."""
        yn_groups = {
            "yn-p1": _yn_group(event_id="E1", player_id="P1", yes={
                "draftkings": {"price": -214, "decimal_odds": 1.467, "validation_status": "VALID"},
            }),
            "yn-p2": _yn_group(event_id="E1", player_id="P2", yes={
                "fanduel": {"price": 120, "decimal_odds": 2.20, "validation_status": "VALID"},
            }),
        }
        ou_key_p1 = _ou_key(player_id="P1")
        ou_key_p2 = _ou_key(player_id="P2")
        ou_groups = {
            ou_key_p1: _ou_group(over={
                "betmgm": {"price": -210, "decimal_odds": 1.476, "line": 0.5,
                           "validation_status": "VALID", "display_side": "OVER"},
            }),
            ou_key_p2: _ou_group(over={
                "bovada": {"price": 130, "decimal_odds": 2.30, "line": 0.5,
                           "validation_status": "VALID", "display_side": "OVER"},
            }),
        }
        yn_type_map = {"batting_strikeouts_yn": _config()}

        _merge_equivalent_yn_ou_books(ou_groups, yn_groups, yn_type_map)

        assert set(ou_groups[ou_key_p1]["over"]) == {"betmgm", "draftkings"}
        assert set(ou_groups[ou_key_p2]["over"]) == {"bovada", "fanduel"}
        assert "fanduel" not in ou_groups[ou_key_p1]["over"]
        assert "draftkings" not in ou_groups[ou_key_p2]["over"]


def _sgo_ou_market(prefix: str, player_id: str, player_name: str,
                    line: float, over_books: dict, under_books: dict) -> dict:
    """One player's O/U market in real SportsGameOdds oddID-keyed shape
    (over_books/under_books: {bookmaker: american_odds}) -- mirrors
    tests/test_player_prop_scanner.py's own helper of the same name."""
    return {
        f"{prefix}-{player_id}-game-ou-over": {
            "playerID": player_id, "playerNames": {"full": player_name, "short": player_name},
            "marketName": f"{player_name} Over/Under",
            "byBookmaker": {
                book: {"overUnder": line, "odds": odds, "available": True}
                for book, odds in over_books.items()
            },
        },
        f"{prefix}-{player_id}-game-ou-under": {
            "playerID": player_id, "playerNames": {"full": player_name, "short": player_name},
            "marketName": f"{player_name} Over/Under",
            "byBookmaker": {
                book: {"overUnder": line, "odds": odds, "available": True}
                for book, odds in under_books.items()
            },
        },
    }


def _sgo_yn_market(prefix: str, player_id: str, player_name: str, yes_books: dict) -> dict:
    """One player's YN market in real SportsGameOdds oddID-keyed shape
    (yes_books: {bookmaker: american_odds})."""
    return {
        f"{prefix}-{player_id}-game-yn-yes": {
            "playerID": player_id, "playerNames": {"full": player_name, "short": player_name},
            "marketName": f"{player_name} Yes/No",
            "byBookmaker": {
                book: {"odds": odds, "available": True}
                for book, odds in yes_books.items()
            },
        },
        f"{prefix}-{player_id}-game-yn-no": {
            "playerID": player_id, "playerNames": {"full": player_name, "short": player_name},
            "marketName": f"{player_name} Yes/No",
            "byBookmaker": {},
        },
    }


class TestMergeIsWiredIntoRunScanEndToEnd:
    """Confirms the single-line wiring in run_scan (calling
    _merge_equivalent_yn_ou_books right after the grouping loop)
    actually takes effect against the real SGO parsing/grouping path,
    not just the isolated unit tests above."""

    def test_yn_only_book_widens_the_ou_book_count_in_a_real_scan(self):
        from unittest.mock import patch, MagicMock
        from src.player_prop_scanner import run_scan

        odds = {}
        odds.update(_sgo_ou_market(
            "batting_strikeouts", "BATTER_1_MLB", "Test Batter", 0.5,
            over_books={"betmgm": -210, "espnbet": -190, "fanduel": -230, "caesars": -218},
            under_books={"betmgm": 155, "espnbet": 150, "fanduel": 163, "caesars": 160},
        ))
        # draftkings quotes ONLY the YN representation for this player --
        # the exact real-world scenario this fix targets.
        odds.update(_sgo_yn_market(
            "batting_strikeouts", "BATTER_1_MLB", "Test Batter",
            yes_books={"draftkings": -214},
        ))
        event = {
            "eventID": "EVENT_MERGE_TEST",
            "teams": {
                "away": {"names": {"long": "Team A"}, "teamID": "TMA"},
                "home": {"names": {"long": "Team B"}, "teamID": "TMB"},
            },
            "odds": odds,
        }

        fake_client = MagicMock()
        fake_client.get_events.return_value = ({"data": [event]}, False)
        with patch("src.player_prop_scanner.SportsGameOddsClient", return_value=fake_client), \
             patch("src.player_prop_scanner.get_connection", return_value=MagicMock()), \
             patch("src.player_prop_scanner.create_run", return_value=None), \
             patch("src.player_prop_scanner.finish_run"), \
             patch("src.player_prop_scanner.save_player_prop_batch"):
            # market_form="all" matches src/daily_pipeline.py's real
            # production default -- both OU and YN rows must be scanned
            # together in the same pass for the merge to have anything
            # to work with (an OU-only or YN-only pass filters the
            # other representation out before grouping even happens).
            result = run_scan(
                league="MLB", mode="all", market="batter_strikeouts", market_form="all",
                fetch_props=False, limit=None,
            )

        opps = [o for o in result["opportunities"] if o["player_id"] == "BATTER_1_MLB"]
        assert opps, "expected at least one strikeouts opportunity for BATTER_1_MLB"
        # draftkings never posted an OU row for this player -- its
        # presence here proves the YN merge actually widened the real
        # book pool the analysis ran against, not just a unit-level dict.
        books_seen = {o["sportsbook"] for o in opps}
        assert "draftkings" in books_seen
