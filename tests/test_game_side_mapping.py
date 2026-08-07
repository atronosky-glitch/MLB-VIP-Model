"""Tests for registry-defined game-side normalization."""

from src.player_prop_scanner import _group_side


def test_game_sides_map_to_generic_analysis_slots():
    assert _group_side("AWAY", "game_moneyline") == "over"
    assert _group_side("HOME", "game_moneyline") == "under"


def test_player_prop_sides_remain_unchanged():
    assert _group_side("OVER", "pitching_strikeouts_ou") == "OVER"
    assert _group_side("UNDER", "pitching_strikeouts_ou") == "UNDER"
