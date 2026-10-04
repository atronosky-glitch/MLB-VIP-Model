"""One card per bet on the customer board.

Found live 2026-10-04: the "Full Board" query returned 25 rows that were only 16
distinct bets, and two identical betrivers HR-over-1.5 rows made Streamlit raise
StreamlitDuplicateElementKey, crashing EV Picks and hiding the track record below it.
"""

from pathlib import Path

from src.pick_dedup import PICK_IDENTITY_FIELDS, dedupe_picks, pick_identity

ROOT = Path(__file__).resolve().parents[1]


def _pick(**over):
    base = {"event_id": "E1", "player_id": "P1", "market_type": "batting_homeRuns_ou",
            "line": 1.5, "side": "OVER", "sportsbook": "betrivers",
            "model_score": 7.0, "ev_pct": 3.0, "scan_timestamp": "2026-10-04T12:00:00Z"}
    base.update(over)
    return base


def test_rescans_of_the_same_bet_collapse_to_one_card():
    rows = [_pick(model_score=8.0), _pick(model_score=7.5, scan_timestamp="2026-10-04T13:00:00Z"),
            _pick(model_score=7.0, scan_timestamp="2026-10-04T14:00:00Z")]
    result = dedupe_picks(rows)
    assert len(result) == 1


def test_best_ranked_row_is_the_one_kept():
    rows = [_pick(model_score=8.0), _pick(model_score=5.0)]        # input is ordered best-first
    assert dedupe_picks(rows)[0]["model_score"] == 8.0


def test_different_book_line_side_or_player_stay_separate():
    rows = [_pick(), _pick(sportsbook="draftkings"), _pick(line=2.5), _pick(side="UNDER"), _pick(player_id="P2")]
    assert len(dedupe_picks(rows)) == 5


def test_order_is_preserved():
    rows = [_pick(player_id="A"), _pick(player_id="B"), _pick(player_id="A"), _pick(player_id="C")]
    assert [r["player_id"] for r in dedupe_picks(rows)] == ["A", "B", "C"]


def test_limit_caps_distinct_bets_not_raw_rows():
    # 40 raw rows but only 3 distinct bets, 2 of them seen many times -> limit applies to distinct
    rows = [_pick(player_id="A")] * 20 + [_pick(player_id="B")] * 19 + [_pick(player_id="C")]
    assert len(dedupe_picks(rows, limit=2)) == 2
    assert len(dedupe_picks(rows, limit=25)) == 3


def test_moneyline_rows_with_no_line_are_deduped():
    rows = [_pick(market_type="game_moneyline", line=None, player_id="GAME")] * 3
    assert len(dedupe_picks(rows)) == 1


def test_no_two_output_rows_share_an_identity():
    rows = [_pick(player_id=str(i % 4), sportsbook="b" + str(i % 3)) for i in range(60)]
    identities = [pick_identity(r) for r in dedupe_picks(rows)]
    assert len(identities) == len(set(identities))


def test_identity_covers_the_fields_the_widget_key_uses():
    # _odds_override_key builds its widget key from these; the dedup must be at least as fine-grained
    # as the key minus scan time, or two surviving rows could still collide.
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    key_fn = source[source.index("def _odds_override_key"): source.index("def _recompute_stake_for_odds")]
    for field in PICK_IDENTITY_FIELDS:
        assert f'pick.get("{field}")' in key_fn, field


def test_customer_page_dedupes_before_truncating_the_full_board():
    source = (ROOT / "src" / "customer_view.py").read_text(encoding="utf-8")
    assert "from src.pick_dedup import dedupe_picks" in source
    assert "research_dicts = dedupe_picks([dict(r) for r in research], limit=25)" in source
    assert "upcoming_dicts = dedupe_picks([dict(r) for r in upcoming])" in source
    # the SQL must fetch more than 25 raw rows, or heavy re-scanning leaves a nearly empty board
    assert "LIMIT 250" in source
