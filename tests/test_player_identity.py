"""Tests for canonical player identity resolution (src/player_identity.py).

No live network calls — ESPNRosterClient is exercised through a fake
subclass with synthetic-but-real-shaped roster data (verified live
2026-08-19 against the real ESPN WNBA roster endpoint).
"""

from __future__ import annotations

import pytest

from src.player_identity import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_UNRESOLVED,
    ESPNRosterClient,
    RosterPlayer,
    normalize_name,
    resolve_player_identity,
)


class TestNormalizeName:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize_name("A'ja Wilson") == "a ja wilson"

    def test_strips_accents(self):
        assert normalize_name("Dāvis Bertāns") == "davis bertans"
        assert normalize_name("Ivana Dojkić") == "ivana dojkic"

    def test_collapses_whitespace(self):
        assert normalize_name("  Shakira   Austin  ") == "shakira austin"

    def test_handles_none_and_empty(self):
        assert normalize_name(None) == ""
        assert normalize_name("") == ""

    def test_periods_in_initials(self):
        assert normalize_name("A.J. Green") == "a j green"


class FakeRosterClient(ESPNRosterClient):
    """No network — returns a fixed roster keyed by team display name."""

    def __init__(self, rosters: dict[str, list[RosterPlayer]]):
        super().__init__()
        self._rosters_by_team_name = rosters
        self._team_ids = {name: f"team-{i}" for i, name in enumerate(rosters)}

    def find_team_id(self, league, team_display_name):
        return self._team_ids.get(team_display_name)

    def get_roster(self, league, team_id):
        for name, tid in self._team_ids.items():
            if tid == team_id:
                return self._rosters_by_team_name[name]
        return []


def _player(name: str, team_id: str, team_name: str, pid: str) -> RosterPlayer:
    return RosterPlayer(
        provider_player_id=pid, display_name=name,
        normalized_name=normalize_name(name), team_id=team_id, team_name=team_name,
    )


def _client():
    return FakeRosterClient({
        "Washington Mystics": [
            _player("Shakira Austin", "team-0", "Washington Mystics", "4398911"),
            _player("Georgia Amoore", "team-0", "Washington Mystics", "4704180"),
            _player("A'ja Wilson", "team-0", "Washington Mystics", "9999001"),
            _player("Marcus Smart Jr.", "team-0", "Washington Mystics", "9999002"),
        ],
        "Toronto Tempo": [
            _player("Sonia Citron", "team-1", "Toronto Tempo", "4433524"),
            _player("Kiki Iriafen", "team-1", "Toronto Tempo", "4898384"),
        ],
    })


class TestResolvePlayerIdentity:
    def test_exact_match_is_high_confidence(self):
        r = resolve_player_identity(
            "Shakira Austin", league="WNBA", home_team="Washington Mystics",
            away_team="Toronto Tempo", client=_client(),
        )
        assert r.confidence == CONFIDENCE_HIGH
        assert r.canonical_player_id == "ESPN_WNBA_4398911"
        assert r.method == "espn_roster_exact_match"
        assert r.team_name == "Washington Mystics"

    def test_scoped_to_the_two_teams_playing(self):
        """A name not on either team's roster must not match some other team."""
        r = resolve_player_identity(
            "Sonia Citron", league="WNBA", home_team="Washington Mystics",
            away_team="Toronto Tempo", client=_client(),
        )
        assert r.confidence == CONFIDENCE_HIGH
        assert r.team_name == "Toronto Tempo"

    def test_suffix_stripped_match_is_medium_confidence(self):
        """Incoming name omits the suffix the roster has (or vice versa)."""
        r = resolve_player_identity(
            "Marcus Smart", league="WNBA", home_team="Washington Mystics",
            away_team="Toronto Tempo", client=_client(),
        )
        assert r.confidence == CONFIDENCE_MEDIUM
        assert r.method == "espn_roster_suffix_stripped_match"

    def test_initial_last_match_is_medium_confidence(self):
        r = resolve_player_identity(
            "A. Wilson", league="WNBA", home_team="Washington Mystics",
            away_team="Toronto Tempo", client=_client(),
        )
        assert r.confidence == CONFIDENCE_MEDIUM
        assert r.canonical_player_id == "ESPN_WNBA_9999001"

    def test_unresolved_when_no_match(self):
        r = resolve_player_identity(
            "Totally Unknown Player", league="WNBA", home_team="Washington Mystics",
            away_team="Toronto Tempo", client=_client(),
        )
        assert r.confidence == CONFIDENCE_UNRESOLVED
        assert r.canonical_player_id is None

    def test_empty_name_is_unresolved(self):
        r = resolve_player_identity(
            "", league="WNBA", home_team="Washington Mystics",
            away_team="Toronto Tempo", client=_client(),
        )
        assert r.confidence == CONFIDENCE_UNRESOLVED
        assert r.method == "empty_name"

    def test_ambiguous_duplicate_names_are_low_confidence_not_guessed(self):
        client = FakeRosterClient({
            "Team A": [
                _player("Jordan Lee", "team-0", "Team A", "1"),
            ],
            "Team B": [
                _player("Jordan Lee", "team-1", "Team B", "2"),
            ],
        })
        r = resolve_player_identity(
            "Jordan Lee", league="WNBA", home_team="Team A",
            away_team="Team B", client=client,
        )
        assert r.confidence == CONFIDENCE_LOW
        assert r.canonical_player_id is None

    def test_missing_team_yields_unresolved_not_a_crash(self):
        r = resolve_player_identity(
            "Shakira Austin", league="WNBA", home_team="Nonexistent Team",
            away_team="Also Nonexistent", client=_client(),
        )
        assert r.confidence == CONFIDENCE_UNRESOLVED
        assert r.method == "no_roster_data"
