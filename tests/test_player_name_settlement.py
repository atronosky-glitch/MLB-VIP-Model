"""Player-name matching for result settlement (MLB / NFL / WNBA).

Found in production 2026-09-26: 1,250 MLB recommendations never settled because
"Yandy Diaz" != "Yandy Díaz" and "Rafael Flores" != "Rafael Flores Jr.".  The fix folds
diacritics and one trailing generational suffix for PLAYER lookup only.  Everything that
was ambiguous or unverifiable must stay unresolved: two players that collapse to one key,
players with no stats, unmapped markets, and provider identity artifacts ("Any", "-DUP",
wrong names on a provider's player id).
"""

import pytest

from src.mlb_results import extract_stat_fact as mlb_fact
from src.name_normalization import normalize_player_name
from src.nfl_results import extract_stat_fact as nfl_fact
from src.wnba_results import extract_stat_fact as wnba_fact


# ── builders ────────────────────────────────────────────────────────────────
def _mlb_feed(*players):
    """players: (id, fullName, batting_stats_dict | None)."""
    entries = {}
    for pid, name, batting in players:
        stats = {"batting": batting} if batting is not None else {}
        entries[f"ID{pid}"] = {"person": {"id": pid, "fullName": name}, "stats": stats}
    return {"gameData": {"status": {"abstractGameState": "Final"}},
            "liveData": {"decisions": {}, "boxscore": {"teams": {
                "away": {"teamStats": {"batting": {"runs": 1}}, "players": entries},
                "home": {"teamStats": {"batting": {"runs": 0}}, "players": {}}}}}}


def _nfl_summary(*athletes):
    """athletes: (id, displayName, rushing_yards)."""
    return {"boxscore": {"players": [{"statistics": [{
        "name": "rushing", "labels": ["CAR", "YDS"],
        "athletes": [{"athlete": {"id": aid, "displayName": name}, "stats": ["10", str(yds)]}
                     for aid, name, yds in athletes]}]}]}}


def _wnba_summary(*athletes):
    """athletes: (id, displayName, points)."""
    return {"boxscore": {"players": [{"statistics": [{
        "labels": ["MIN", "PTS"],
        "athletes": [{"athlete": {"id": aid, "displayName": name}, "stats": ["30", str(pts)]}
                     for aid, name, pts in athletes]}]}]}}


HR = {"homeRuns": 1, "hits": 2}
MLB_HR = "batting_homeRuns_ou"


# ── normalizer ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b", [
    ("José Ramírez", "Jose Ramirez"),
    ("Yandy Díaz", "Yandy Diaz"),
    ("Pedro Pagés", "Pedro Pages"),
    ("Rafael Flores Jr.", "Rafael Flores"),
    ("Ken Griffey Sr.", "Ken Griffey"),
    ("Cal Ripken II", "Cal Ripken"),
    ("Player Name III", "Player Name"),
    ("Player Name IV", "Player Name"),
    ("  RAFAEL   FLORES,  JR ", "rafael flores"),
])
def test_normalizer_folds_diacritics_and_one_trailing_suffix(a, b):
    assert normalize_player_name(a) == normalize_player_name(b)


@pytest.mark.parametrize("a,b", [
    ("Willi Castro", "Wilmer Castro"),        # different people
    ("Leonardo Bernal", "Leo Bernal"),         # nickname: never guessed
    ("Ryan Waldschmidt Any", "Ryan Waldschmidt"),   # provider artifact is NOT stripped
    ("Harry Ford-DUP", "Harry Ford"),          # provider artifact is NOT stripped
    ("Kenley Jansen", "Danny Jansen"),         # identity mismatch is NOT bridged
    ("Jr Smith", "Smith"),                     # suffix only when trailing
])
def test_normalizer_does_not_guess(a, b):
    assert normalize_player_name(a) != normalize_player_name(b)


def test_normalizer_handles_empty():
    assert normalize_player_name(None) == "" and normalize_player_name("") == ""


# ── MLB ─────────────────────────────────────────────────────────────────────
def test_mlb_accented_box_score_name_settles_from_ascii_recommendation():
    feed = _mlb_feed((1, "Yandy Díaz", HR))
    fact = mlb_fact(feed, {"player_name": "Yandy Diaz", "market_type": MLB_HR})
    assert fact["value"] == 1.0 and fact["player_id"] == "1"


def test_mlb_suffix_in_box_score_settles_from_plain_recommendation():
    feed = _mlb_feed((2, "Rafael Flores Jr.", HR))
    assert mlb_fact(feed, {"player_name": "Rafael Flores", "market_type": MLB_HR})["value"] == 1.0


def test_mlb_reverse_direction_also_matches():
    feed = _mlb_feed((3, "Jose Ramirez", HR))
    assert mlb_fact(feed, {"player_name": "José Ramírez Jr.", "market_type": MLB_HR})["value"] == 1.0


def test_mlb_two_players_collapsing_to_one_key_stay_unresolved():
    feed = _mlb_feed((4, "Rafael Flores", {"homeRuns": 0}), (5, "Rafael Flores Jr.", {"homeRuns": 1}))
    assert mlb_fact(feed, {"player_name": "Rafael Flores", "market_type": MLB_HR}) is None


def test_mlb_accent_collision_stays_unresolved():
    feed = _mlb_feed((6, "Luis García", {"homeRuns": 1}), (7, "Luis Garcia", {"homeRuns": 0}))
    assert mlb_fact(feed, {"player_name": "Luis Garcia", "market_type": MLB_HR}) is None


def test_mlb_player_with_no_stats_stays_unresolved():
    feed = _mlb_feed((8, "Ryan Waldschmidt", None))
    assert mlb_fact(feed, {"player_name": "Ryan Waldschmidt", "market_type": "batting_hits_ou"}) is None


def test_mlb_unmapped_market_stays_unresolved():
    feed = _mlb_feed((9, "Kyle Schwarber", HR))
    assert mlb_fact(feed, {"player_name": "Kyle Schwarber", "market_type": "batting_firstHomeRun_yn"}) is None


@pytest.mark.parametrize("provider_name", ["Ryan Waldschmidt Any", "Harry Ford-DUP", "Carlos Cortes Any"])
def test_mlb_provider_artifact_names_stay_unresolved(provider_name):
    base = provider_name.replace(" Any", "").replace("-DUP", "")
    feed = _mlb_feed((10, base, HR))
    assert mlb_fact(feed, {"player_name": provider_name, "market_type": MLB_HR}) is None


def test_mlb_known_bad_provider_identity_stays_unresolved():
    # provider labels player id DANNY_JANSEN_1_MLB with the name "Kenley Jansen"
    feed = _mlb_feed((11, "Danny Jansen", HR))
    rec = {"player_name": "Kenley Jansen", "player_id": "DANNY_JANSEN_1_MLB", "market_type": MLB_HR}
    assert mlb_fact(feed, rec) is None


def test_mlb_exact_plain_ascii_match_unchanged():
    feed = _mlb_feed((12, "Aaron Judge", HR))
    assert mlb_fact(feed, {"player_name": "Aaron Judge", "market_type": MLB_HR})["value"] == 1.0


# ── NFL ─────────────────────────────────────────────────────────────────────
NFL_RUSH = "rushing_yards_ou"


def test_nfl_accent_and_suffix_settle():
    summary = _nfl_summary(("1", "Odell Beckham Jr.", 44), ("2", "Josh Jacobs", 88))
    assert nfl_fact(summary, {"player_name": "Odell Beckham", "market_type": NFL_RUSH})["value"] == 44.0
    accented = _nfl_summary(("3", "Ka'imi Fairbairn", 0), ("4", "José Tést", 61))
    assert nfl_fact(accented, {"player_name": "Jose Test", "market_type": NFL_RUSH})["value"] == 61.0


def test_nfl_collision_stays_unresolved():
    summary = _nfl_summary(("1", "Kenneth Walker", 10), ("2", "Kenneth Walker III", 90))
    assert nfl_fact(summary, {"player_name": "Kenneth Walker", "market_type": NFL_RUSH}) is None


def test_nfl_unmapped_market_stays_unresolved():
    summary = _nfl_summary(("1", "Josh Jacobs", 88))
    assert nfl_fact(summary, {"player_name": "Josh Jacobs", "market_type": "punting_yards_ou"}) is None


def test_nfl_player_absent_stays_unresolved():
    assert nfl_fact(_nfl_summary(("1", "Josh Jacobs", 88)), {"player_name": "Nobody Here", "market_type": NFL_RUSH}) is None


# ── WNBA ────────────────────────────────────────────────────────────────────
WNBA_PTS = "player_points_ou"


def test_wnba_accent_and_suffix_settle():
    summary = _wnba_summary(("1", "Nneka Ogwumike", 20), ("2", "Kayla McBride", 15))
    assert wnba_fact(summary, {"player_name": "Nnéka Ogwumike", "market_type": WNBA_PTS})["value"] == 20.0
    suffixed = _wnba_summary(("3", "Some Player Jr.", 9))
    assert wnba_fact(suffixed, {"player_name": "Some Player", "market_type": WNBA_PTS})["value"] == 9.0


def test_wnba_collision_stays_unresolved():
    summary = _wnba_summary(("1", "Alex Smith", 10), ("2", "Alex Smith Jr.", 12))
    assert wnba_fact(summary, {"player_name": "Alex Smith", "market_type": WNBA_PTS}) is None


def test_wnba_unmapped_market_and_absent_player_stay_unresolved():
    summary = _wnba_summary(("1", "Alex Smith", 10))
    assert wnba_fact(summary, {"player_name": "Alex Smith", "market_type": "player_steals_blocks_ou"}) is None
    assert wnba_fact(summary, {"player_name": "Nobody", "market_type": WNBA_PTS}) is None


# ── team matching must be unchanged ─────────────────────────────────────────
def test_team_name_normalization_is_untouched():
    import src.mlb_results as mlb
    import src.nfl_results as nfl
    import src.wnba_results as wnba
    for module in (mlb, nfl, wnba):
        assert module.normalize_name("Tampa Bay Rays") == "tampa bay rays"
        assert module.normalize_name("Díaz Jr.") == "d az jr"      # legacy behavior, teams only
