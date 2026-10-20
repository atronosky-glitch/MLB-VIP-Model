"""Player-name punctuation in the Pinnacle join (offline only: no API calls, no database).

Pinnacle posts "JT Ginn" while sportsbook rows carry "J.T. Ginn" (real example from a 2026-08-26 cached Pinnacle
response). normalize_name kept periods, so the exact player join silently missed. Periods and apostrophes are now
removed; everything else (accents, hyphens, suffix words, word boundaries, exact lines, teams) is unchanged.
"""

import pytest

from src.pinnacle_feed import (
    PinnacleProp, build_pinnacle_lookup, inject_pinnacle_reference, match_pinnacle, normalize_name,
    normalize_team_name,
)

HOME, AWAY = "Athletics", "Minnesota Twins"      # as in the captured response
TYPOGRAPHIC_APOSTROPHE = chr(0x2019)


def _prop(player: str, line: float = 16.5) -> PinnacleProp:
    return PinnacleProp(
        home_name=HOME, away_name=AWAY, player_name=player, unit="PitchingOuts", line=line,
        over_decimal=1.99, under_decimal=1.76, over_american=-101, under_american=-132,
        last_updated=None, source="odds_api_pinnacle",
    )


def _match(lookup, player: str, line: float = 16.5):
    return match_pinnacle(lookup, home_name=HOME, away_name=AWAY, player_name=player,
                          market_type="pitching_outs_ou", line=line, league="MLB")


class TestNormalizeName:
    @pytest.mark.parametrize("a, b", [
        ("J.T. Ginn", "JT Ginn"),
        ("A.J. Brown", "AJ Brown"),
        ("D'Andre Swift", "DAndre Swift"),
        ("D" + TYPOGRAPHIC_APOSTROPHE + "Andre Swift", "DAndre Swift"),
        ("Ja'Marr Chase", "JaMarr Chase"),
        ("J.T. Ginn", "j.t. ginn"),
    ])
    def test_punctuation_variants_normalize_identically(self, a, b):
        assert normalize_name(a) == normalize_name(b)

    @pytest.mark.parametrize("name, expected", [
        ("J.T. Ginn", "jt ginn"),
        ("A.J. Brown", "aj brown"),
        ("D'Andre Swift", "dandre swift"),
    ])
    def test_expected_normal_forms(self, name, expected):
        assert normalize_name(name) == expected

    @pytest.mark.parametrize("a, b", [
        ("JT Ginn", "JT Gin"),                       # one letter off is a different player
        ("J.T. Ginn", "J T Ginn"),                   # periods are removed, not turned into spaces
        ("Marvin Harrison Jr.", "Marvin Harrison"),  # suffix words are not dropped
        ("Marvin Harrison Jr.", "Marvin Harrison Sr."),
        ("Kenneth Walker III", "Kenneth Walker II"),
        ("Jean-Luc Picard", "Jean Luc Picard"),      # hyphens untouched
        ("Jean-Luc Picard", "JeanLuc Picard"),
    ])
    def test_distinct_names_stay_distinct(self, a, b):
        assert normalize_name(a) != normalize_name(b)

    def test_existing_behaviour_is_unchanged(self):
        assert normalize_name("José Ramírez") == "jose ramirez"
        assert normalize_name("  Shota   Imanaga ") == "shota imanaga"
        assert normalize_name("Jean-Luc Picard") == "jean-luc picard"
        assert normalize_name("Marvin Harrison Jr.") == "marvin harrison jr"      # suffix kept, only its period goes
        assert normalize_name("") == "" and normalize_name(None) == ""

    def test_team_names_keep_their_punctuation(self):
        """normalize_team_name used to share normalize_name; it must not change behaviour as a side effect."""
        assert normalize_team_name("St. Louis Cardinals") == "st. louis cardinals"
        assert normalize_team_name("Kansas City Royals") == "kansas city royals"
        assert normalize_team_name("  Athletics ") == "athletics"


class TestPinnacleJoinUsesTheNewNormalization:
    @pytest.mark.parametrize("sportsbook_name, pinnacle_name", [
        ("J.T. Ginn", "JT Ginn"),
        ("A.J. Brown", "AJ Brown"),
        ("D'Andre Swift", "DAndre Swift"),
        ("JT Ginn", "J.T. Ginn"),                    # and the reverse direction
    ])
    def test_variant_spellings_now_match_at_the_exact_line(self, sportsbook_name, pinnacle_name):
        lookup = build_pinnacle_lookup([_prop(pinnacle_name)])
        found = _match(lookup, sportsbook_name)
        assert found is not None and found.player_name == pinnacle_name

    def test_negative_case_stays_unmatched(self):
        lookup = build_pinnacle_lookup([_prop("JT Ginn")])
        assert _match(lookup, "JT Gin") is None
        assert _match(lookup, "J.T. Gin") is None

    def test_exact_line_matching_is_unchanged(self):
        lookup = build_pinnacle_lookup([_prop("JT Ginn", line=16.5)])
        assert _match(lookup, "J.T. Ginn", line=16.5) is not None
        assert _match(lookup, "J.T. Ginn", line=15.5) is None
        assert _match(lookup, "J.T. Ginn", line=17.5) is None

    def test_other_teams_do_not_cross_match(self):
        lookup = build_pinnacle_lookup([_prop("JT Ginn")])
        assert match_pinnacle(lookup, home_name="Detroit Tigers", away_name="Kansas City Royals",
                              player_name="J.T. Ginn", market_type="pitching_outs_ou", line=16.5, league="MLB") is None

    def test_injection_attaches_the_pinnacle_reference_to_the_variant_named_group(self):
        group = {
            "over": {"draftkings": {"price": -110, "decimal_odds": 1.9091, "line": 16.5, "validation_status": "VALID"}},
            "under": {"draftkings": {"price": -110, "decimal_odds": 1.9091, "line": 16.5, "validation_status": "VALID"}},
            "line": 16.5, "player_id": "JT_GINN_1_MLB", "player_name": "J.T. Ginn",
            "event_id": "ev1", "market_type": "pitching_outs_ou",
        }
        event_map = {"ev1": {"home_name": HOME, "away_name": AWAY, "start_time": "2026-08-27T01:40:00Z"}}
        injected, stale = inject_pinnacle_reference({"k": group}, event_map, build_pinnacle_lookup([_prop("JT Ginn")]),
                                                     league="MLB")
        assert (injected, stale) == (1, 0)
        assert group["over"]["pinnacle"]["price"] == -101 and group["under"]["pinnacle"]["price"] == -132
        assert group["over"]["pinnacle"]["line"] == 16.5
