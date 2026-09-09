"""Tests for src/line_plausibility.py."""

from src.line_plausibility import consensus_lines, is_plausible_line, MAX_LINE_DEVIATION


def _row(event_id, player_id, market_type, line):
    return {"event_id": event_id, "player_id": player_id, "market_type": market_type, "line": line}


def test_consensus_lines_is_the_median_per_event_player_market():
    rows = [
        _row("E1", "GAME", "game_total_ou", 4.5),
        _row("E1", "GAME", "game_total_ou", 8.5),
        _row("E1", "GAME", "game_total_ou", 8.5),
        _row("E1", "GAME", "game_total_ou", 9.0),
    ]
    result = consensus_lines(rows)
    assert result[("E1", "GAME", "game_total_ou")] == 8.5


def test_consensus_lines_scoped_per_event_and_market():
    rows = [
        _row("E1", "GAME", "game_total_ou", 8.5),
        _row("E2", "GAME", "game_total_ou", 3.5),
        _row("E1", "P1", "batting_totalBases_ou", 1.5),
    ]
    result = consensus_lines(rows)
    assert result[("E1", "GAME", "game_total_ou")] == 8.5
    assert result[("E2", "GAME", "game_total_ou")] == 3.5
    assert result[("E1", "P1", "batting_totalBases_ou")] == 1.5


def test_consensus_lines_ignores_rows_with_no_line():
    rows = [
        {"event_id": "E1", "player_id": "GAME", "market_type": "game_moneyline", "line": None},
    ]
    assert consensus_lines(rows) == {}


def test_is_plausible_line_uses_market_specific_threshold():
    assert MAX_LINE_DEVIATION["game_total_ou"] == 2.0
    assert is_plausible_line("game_total_ou", 8.5, 8.5) is True
    assert is_plausible_line("game_total_ou", 10.5, 8.5) is True  # exactly at the boundary
    assert is_plausible_line("game_total_ou", 4.5, 8.5) is False  # the real bug case
    assert is_plausible_line("game_total_ou", 7.5, 8.5) is True


def test_is_plausible_line_falls_back_to_default_threshold_for_unlisted_markets():
    from src.line_plausibility import DEFAULT_MAX_DEVIATION
    assert DEFAULT_MAX_DEVIATION == 1.5
    assert is_plausible_line("batting_totalBases_ou", 1.5, 1.5) is True
    assert is_plausible_line("batting_totalBases_ou", 3.5, 1.5) is False
