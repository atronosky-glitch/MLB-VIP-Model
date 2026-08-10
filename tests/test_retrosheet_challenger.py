"""Tests for chronological Retrosheet challenger loading."""

import csv
import zipfile

from src.retrosheet_challenger import evaluate_pitcher_records, load_pitcher_game_records


def _write_csv(archive, name, fields, rows):
    import io
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    archive.writestr(name, output.getvalue())


def test_loader_builds_pregame_features_without_lookahead(tmp_path):
    path = tmp_path / "retro.zip"
    with zipfile.ZipFile(path, "w") as archive:
        _write_csv(archive, "pitching.csv",
                   ["gid", "id", "team", "opp", "stattype", "p_k", "p_bfp", "p_gs", "date", "gametype"], [
                       {"gid": "G1", "id": "P1", "team": "AAA", "opp": "BBB", "stattype": "value", "p_k": "6", "p_bfp": "24", "p_gs": "1", "date": "20210101", "gametype": "regular"},
                       {"gid": "G2", "id": "P1", "team": "AAA", "opp": "BBB", "stattype": "value", "p_k": "8", "p_bfp": "27", "p_gs": "1", "date": "20210108", "gametype": "regular"},
                   ])
        _write_csv(archive, "batting.csv",
                   ["gid", "team", "stattype", "b_pa", "b_k"], [
                       {"gid": "G1", "team": "BBB", "stattype": "value", "b_pa": "30", "b_k": "8"},
                       {"gid": "G2", "team": "BBB", "stattype": "value", "b_pa": "30", "b_k": "9"},
                   ])
    records = load_pitcher_game_records(path)
    assert len(records) == 2
    assert records[1]["games_started_before"] == 1
    assert records[1]["actual_strikeouts"] == 8
    assert evaluate_pitcher_records(records)["sample_size"] == 2
