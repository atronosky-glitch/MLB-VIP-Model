"""Retrosheet CSV loader for independent strikeout-model research.

The loader creates chronological, pregame-style pitcher records from a
Retrosheet CSV ZIP. It never reads sportsbook prices and only updates rolling
features after each game's outcome, preventing look-ahead leakage.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from pathlib import Path

from .strikeout_challenger import DEFAULT_PRIOR_K_RATE, DEFAULT_PRIOR_BATTERS


def _rows(archive: zipfile.ZipFile, filename: str) -> list[dict]:
    with archive.open(filename) as raw:
        return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8")))


def load_pitcher_game_records(zip_path: str | Path, *, start_year: int | None = None) -> list[dict]:
    """Build chronological independent pitcher-game feature records."""
    with zipfile.ZipFile(zip_path) as archive:
        pitching = _rows(archive, "pitching.csv")
        batting = _rows(archive, "batting.csv")

    # Opponent strikeout rate is built only from games before the pitcher row.
    team_k = defaultdict(int)
    team_pa = defaultdict(int)
    pitcher_k = defaultdict(int)
    pitcher_bf = defaultdict(int)
    pitcher_starts = defaultdict(int)
    pitcher_start_bf = defaultdict(int)
    records: list[dict] = []
    batting_by_game: dict[str, list[dict]] = defaultdict(list)
    for row in batting:
        batting_by_game[row.get("gid", "")].append(row)

    ordered = sorted(pitching, key=lambda row: (row.get("date", ""), row.get("gid", "")))
    for row in ordered:
        if row.get("stattype") != "value" or row.get("gametype") not in ("regular", "playoff", ""):
            continue
        try:
            year = int((row.get("date") or "")[:4])
            strikeouts = float(row.get("p_k") or 0)
            batters_faced = float(row.get("p_bfp") or 0)
            starts = float(row.get("p_gs") or 0)
        except (TypeError, ValueError):
            continue
        if start_year is not None and year < start_year:
            continue
        pitcher_id = row.get("id", "")
        opponent = row.get("opp", "")
        prior_pitcher_rate = (
            (pitcher_k[pitcher_id] + DEFAULT_PRIOR_K_RATE * DEFAULT_PRIOR_BATTERS)
            / (pitcher_bf[pitcher_id] + DEFAULT_PRIOR_BATTERS)
        )
        opponent_rate = (
            (team_k[opponent] + DEFAULT_PRIOR_K_RATE * DEFAULT_PRIOR_BATTERS)
            / (team_pa[opponent] + DEFAULT_PRIOR_BATTERS)
        )
        expected_bf = (
            pitcher_start_bf[pitcher_id] / pitcher_starts[pitcher_id]
            if pitcher_starts[pitcher_id] else 27.0
        )
        rate = (prior_pitcher_rate + opponent_rate) / 2.0
        if starts:
            records.append({
            "gid": row.get("gid"), "date": row.get("date"), "pitcher_id": pitcher_id,
            "team": row.get("team"), "opponent": opponent,
            "pitcher_rate": prior_pitcher_rate, "opponent_k_rate": opponent_rate,
            "expected_batters_faced": expected_bf,
            "expected_strikeouts": rate * expected_bf,
            "actual_strikeouts": strikeouts,
            "games_started_before": pitcher_starts[pitcher_id],
            })

        pitcher_k[pitcher_id] += strikeouts
        pitcher_bf[pitcher_id] += batters_faced
        pitcher_starts[pitcher_id] += starts
        if starts:
            pitcher_start_bf[pitcher_id] += batters_faced
        for batter in batting_by_game.get(row.get("gid", ""), []):
            if batter.get("stattype") != "value":
                continue
            try:
                team_k[batter.get("team", "")] += float(batter.get("b_k") or 0)
                team_pa[batter.get("team", "")] += float(batter.get("b_pa") or 0)
            except (TypeError, ValueError):
                continue
    return records


def evaluate_pitcher_records(records: list[dict]) -> dict:
    """Return descriptive, non-betting metrics for the independent baseline."""
    if not records:
        return {"sample_size": 0, "mae": None, "bias": None}
    errors = [r["expected_strikeouts"] - r["actual_strikeouts"] for r in records]
    return {
        "sample_size": len(records),
        "mae": round(sum(abs(error) for error in errors) / len(errors), 6),
        "bias": round(sum(errors) / len(errors), 6),
        "feature_source": "Retrosheet",
    }
